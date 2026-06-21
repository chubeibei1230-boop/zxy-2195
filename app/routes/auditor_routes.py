from datetime import datetime
from flask import Blueprint, request, jsonify

from app.db import get_db
from app.utils import (
    role_required, get_current_user, row_to_dict, rows_to_list,
    ensure_single_active_task, update_paper_status, check_score_diff,
    STATUS_MAP, detect_anomalies, APPEAL_STATUS_MAP, APPEAL_TYPE_MAP
)

auditor_bp = Blueprint('auditor', __name__, url_prefix='/api/auditor')


@auditor_bp.route('/dashboard', methods=['GET'])
@role_required('auditor', 'admin')
def dashboard():
    user = get_current_user()
    db = get_db()
    uid = user['id']

    stats = db.execute("""
        SELECT
            COUNT(CASE WHEN t.status = 'pending_audit' THEN 1 END) as pending_audit,
            COUNT(CASE WHEN t.status = 'diff_pending' THEN 1 END) as diff_pending,
            COUNT(CASE WHEN t.status = 'finalized' AND t.assignee_id = ? THEN 1 END) as finalized_week,
            COUNT(CASE WHEN r.initial_score IS NOT NULL AND r.audit_score IS NOT NULL
                        AND ABS(r.initial_score - r.audit_score) >= 5 THEN 1 END) as diff_over_threshold,
            COUNT(*) as total
        FROM tasks t
        LEFT JOIN reviews r ON r.task_id = t.id AND r.review_type = 'audit'
        WHERE t.task_type = 'audit'
          AND (t.assignee_id = ? OR t.assignee_id IS NULL)
          AND t.created_at >= CURRENT_DATE - INTERVAL 30 DAY
    """, [uid, uid]).fetchone()

    return jsonify({
        "user": user,
        "stats": dict(stats)
    }), 200


@auditor_bp.route('/tasks', methods=['GET'])
@role_required('auditor', 'admin')
def list_tasks():
    user = get_current_user()
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    status = request.args.get('status')
    only_mine = request.args.get('only_mine', 'true').lower() == 'true'

    base_query = """
        FROM tasks t
        LEFT JOIN papers p ON t.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN review_appeals ra ON t.appeal_id = ra.id
        WHERE t.task_type = 'audit'
    """
    params = []
    if only_mine:
        base_query += " AND (t.assignee_id = ? OR t.status IN ('pending_audit', 'diff_pending'))"
        params.append(user['id'])
    if status:
        base_query += " AND t.status = ?"
        params.append(status)

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT t.*, p.paper_number, p.candidate_name, p.candidate_id,
               p.paper_content, p.storage_path, p.current_status as paper_status,
               p.is_reviewing, p.appeal_count,
               b.batch_name, b.batch_code, qg.group_name as question_group_name,
               qg.max_score, qg.pass_score,
               ra.id as appeal_id, ra.status as appeal_status, ra.appeal_type,
               ra.priority
        {base_query}
        ORDER BY CASE WHEN t.status = 'diff_pending' THEN 0
                      WHEN t.status = 'pending_audit' THEN 1
                      ELSE 2 END, t.assigned_at ASC NULLS FIRST
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = STATUS_MAP.get(d['status'], d['status'])
        d['paper_status_name'] = STATUS_MAP.get(d['paper_status'], d['paper_status'])
        if d.get('appeal_status'):
            d['appeal_status_name'] = APPEAL_STATUS_MAP.get(d['appeal_status'], d['appeal_status'])
        if d.get('appeal_type'):
            d['appeal_type_name'] = APPEAL_TYPE_MAP.get(d['appeal_type'], d['appeal_type'])

        orig_reviewer = db.execute("""
            SELECT u.id, u.username, u.real_name
            FROM users u
            INNER JOIN tasks t2 ON t2.assignee_id = u.id
            WHERE t2.paper_id = ? AND t2.task_type = 'review'
            LIMIT 1
        """, [d['paper_id']]).fetchone()
        if orig_reviewer:
            d['original_reviewer'] = dict(orig_reviewer)

        init_review = db.execute("""
            SELECT r.*, u.real_name as reviewer_name, u.username as reviewer_username
            FROM reviews r
            LEFT JOIN users u ON r.reviewer_id = u.id
            WHERE r.paper_id = ? AND r.review_type = 'initial'
            ORDER BY r.id DESC LIMIT 1
        """, [d['paper_id']]).fetchone()
        if init_review:
            d['initial_review'] = dict(init_review)

        audit_review = db.execute("""
            SELECT * FROM reviews WHERE task_id = ? AND review_type = 'audit' LIMIT 1
        """, [d['id']]).fetchone()
        if audit_review:
            d['audit_review'] = dict(audit_review)

        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@auditor_bp.route('/tasks/<int:task_id>/accept', methods=['POST'])
@role_required('auditor', 'admin')
def accept_task(task_id):
    user = get_current_user()
    db = get_db()

    task = db.execute("""
        SELECT id, paper_id, status, assignee_id, is_active
        FROM tasks WHERE id = ? AND task_type = 'audit'
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task['assignee_id'] and task['assignee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "该任务已被他人领取"}), 400
    if not task['is_active']:
        return jsonify({"error": "任务已失效"}), 400
    if task['status'] not in ('pending_audit', 'diff_pending', 'suspended'):
        return jsonify({"error": f"当前状态{task['status']}无法领取"}), 400

    now = datetime.now()
    db.execute("""
        UPDATE tasks SET assignee_id = ?, started_at = ?, status = 'pending_audit'
        WHERE id = ?
    """, [user['id'], now, task_id])

    existing = db.execute("""
        SELECT id FROM reviews WHERE task_id = ? AND review_type = 'audit'
    """, [task_id]).fetchone()
    if not existing:
        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type)
            VALUES (?, ?, ?, 'audit')
        """, [task_id, task['paper_id'], user['id']])

    update_paper_status(db, task['paper_id'], 'pending_audit')
    db.commit()

    return jsonify({"message": "任务已领取，开始复核"}), 200


@auditor_bp.route('/tasks/<int:task_id>/submit', methods=['POST'])
@role_required('auditor', 'admin')
def submit_audit(task_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    audit_score = data.get('audit_score')
    diff_reason = data.get('diff_reason', '')
    final_score = data.get('final_score')
    handling_opinion = data.get('handling_opinion', '')
    auto_finalize = data.get('auto_finalize', False)

    if audit_score is None:
        return jsonify({"error": "复核分为必填项"}), 400

    try:
        audit_score = float(audit_score)
    except (ValueError, TypeError):
        return jsonify({"error": "复核分必须为数字"}), 400

    if final_score is not None:
        try:
            final_score = float(final_score)
        except (ValueError, TypeError):
            return jsonify({"error": "最终分必须为数字"}), 400

    task = db.execute("""
        SELECT t.id, t.paper_id, t.status, t.assignee_id, t.is_active, p.question_group_id
        FROM tasks t
        LEFT JOIN papers p ON t.paper_id = p.id
        WHERE t.id = ? AND t.task_type = 'audit'
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task['assignee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此任务"}), 403
    if not task['is_active']:
        return jsonify({"error": "任务已失效"}), 400
    if task['status'] not in ('pending_audit', 'diff_pending'):
        return jsonify({"error": f"当前状态{task['status']}无法提交"}), 400

    qg = db.execute("SELECT max_score FROM question_groups WHERE id = ?", [task['question_group_id']]).fetchone()
    if qg and (audit_score < 0 or audit_score > float(qg['max_score'])):
        return jsonify({"error": f"复核分应在0到{qg['max_score']}之间"}), 400

    init_review = db.execute("""
        SELECT initial_score FROM reviews
        WHERE paper_id = ? AND review_type = 'initial'
        ORDER BY id DESC LIMIT 1
    """, [task['paper_id']]).fetchone()

    initial_score = init_review['initial_score'] if init_review else None
    is_diff, diff_val = check_score_diff(initial_score, audit_score) if initial_score is not None else (False, 0)

    now = datetime.now()

    existing_audit = db.execute("""
        SELECT id FROM reviews WHERE task_id = ? AND review_type = 'audit'
    """, [task_id]).fetchone()

    new_status = 'diff_pending' if is_diff and not auto_finalize else 'pending_audit'
    final_final_score = None
    if auto_finalize:
        final_final_score = final_score if final_score is not None else audit_score
        new_status = 'finalized'
    elif not is_diff:
        final_final_score = final_score if final_score is not None else audit_score
        new_status = 'finalized'

    if existing_audit:
        db.execute("""
            UPDATE reviews SET
                audit_score = ?, diff_reason = ?, final_score = ?,
                handling_opinion = ?, updated_at = ?
            WHERE id = ?
        """, [audit_score, diff_reason, final_final_score, handling_opinion, now, existing_audit['id']])
    else:
        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type,
                audit_score, diff_reason, final_score, handling_opinion)
            VALUES (?, ?, ?, 'audit', ?, ?, ?, ?)
        """, [task_id, task['paper_id'], user['id'], audit_score, diff_reason, final_final_score, handling_opinion])

    db.execute("""
        UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?
    """, [new_status, now if new_status == 'finalized' else None, task_id])

    if new_status == 'finalized':
        update_paper_status(db, task['paper_id'], 'finalized')
        db.execute("UPDATE tasks SET is_active = false WHERE paper_id = ?", [task['paper_id']])
    elif is_diff:
        update_paper_status(db, task['paper_id'], 'diff_pending')

    detect_anomalies(db)
    db.commit()

    result = {
        "message": "复核已提交",
        "audit_score": audit_score,
        "initial_score": initial_score,
        "score_diff": diff_val,
        "is_diff_over_threshold": is_diff,
        "new_status": new_status,
        "status_name": STATUS_MAP.get(new_status, new_status)
    }
    if final_final_score is not None:
        result["final_score"] = final_final_score

    return jsonify(result), 200


@auditor_bp.route('/tasks/<int:task_id>/save-draft', methods=['POST'])
@role_required('auditor', 'admin')
def save_draft(task_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    audit_score = data.get('audit_score')
    diff_reason = data.get('diff_reason', '')
    handling_opinion = data.get('handling_opinion', '')

    task = db.execute("""
        SELECT id, paper_id, status, assignee_id, is_active
        FROM tasks WHERE id = ? AND task_type = 'audit'
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task['assignee_id'] and task['assignee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此任务"}), 403
    if not task['is_active']:
        return jsonify({"error": "任务已失效"}), 400

    existing_audit = db.execute("""
        SELECT id FROM reviews WHERE task_id = ? AND review_type = 'audit'
    """, [task_id]).fetchone()

    now = datetime.now()
    if existing_audit:
        db.execute("""
            UPDATE reviews SET
                audit_score = ?, diff_reason = ?, handling_opinion = ?, updated_at = ?
            WHERE id = ?
        """, [audit_score, diff_reason, handling_opinion, now, existing_audit['id']])
    else:
        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type,
                audit_score, diff_reason, handling_opinion)
            VALUES (?, ?, ?, 'audit', ?, ?, ?)
        """, [task_id, task['paper_id'], user['id'], audit_score, diff_reason, handling_opinion])
    db.commit()

    return jsonify({"message": "草稿已保存"}), 200


@auditor_bp.route('/diff-pending', methods=['GET'])
@role_required('auditor', 'admin')
def list_diff_pending():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    batch_id = request.args.get('batch_id', type=int)

    base_query = """
        FROM papers p
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        WHERE p.current_status = 'diff_pending'
    """
    params = []
    if batch_id:
        base_query += " AND p.batch_id = ?"
        params.append(batch_id)

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT p.id, p.paper_number, p.candidate_name, p.candidate_id,
               p.current_status, b.batch_name, qg.group_name as question_group_name,
               qg.max_score
        {base_query}
        ORDER BY p.id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = STATUS_MAP.get(d['current_status'], d['current_status'])

        init_r = db.execute("""
            SELECT initial_score, deduction_reason, difficulty_flag, completion_note,
                   u.real_name as reviewer_name
            FROM reviews r
            LEFT JOIN users u ON r.reviewer_id = u.id
            WHERE r.paper_id = ? AND r.review_type = 'initial'
            ORDER BY r.id DESC LIMIT 1
        """, [d['id']]).fetchone()
        audit_r = db.execute("""
            SELECT audit_score, diff_reason, handling_opinion, u.real_name as auditor_name
            FROM reviews r
            LEFT JOIN users u ON r.reviewer_id = u.id
            WHERE r.paper_id = ? AND r.review_type = 'audit'
            ORDER BY r.id DESC LIMIT 1
        """, [d['id']]).fetchone()

        if init_r:
            d['initial_review'] = dict(init_r)
        if audit_r:
            d['audit_review'] = dict(audit_r)
        if init_r and audit_r and init_r['initial_score'] is not None and audit_r['audit_score'] is not None:
            d['score_diff'] = abs(float(init_r['initial_score']) - float(audit_r['audit_score']))

        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@auditor_bp.route('/diff-pending/<int:paper_id>/resolve', methods=['POST'])
@role_required('auditor', 'admin')
def resolve_diff(paper_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    final_score = data.get('final_score')
    handling_opinion = data.get('handling_opinion', '')

    if final_score is None:
        return jsonify({"error": "最终分必填"}), 400

    try:
        final_score = float(final_score)
    except (ValueError, TypeError):
        return jsonify({"error": "最终分必须为数字"}), 400

    paper = db.execute("SELECT id, current_status FROM papers WHERE id = ?", [paper_id]).fetchone()
    if not paper:
        return jsonify({"error": "试卷不存在"}), 404
    if paper['current_status'] not in ('diff_pending',):
        return jsonify({"error": f"当前状态{paper['current_status']}不允许处理差异"}), 400

    now = datetime.now()

    existing_audit = db.execute("""
        SELECT r.id, t.id as task_id
        FROM reviews r
        LEFT JOIN tasks t ON r.task_id = t.id
        WHERE r.paper_id = ? AND r.review_type = 'audit' AND t.task_type = 'audit'
        ORDER BY r.id DESC LIMIT 1
    """, [paper_id]).fetchone()

    if existing_audit:
        db.execute("""
            UPDATE reviews SET final_score = ?, handling_opinion = ?, updated_at = ? WHERE id = ?
        """, [final_score, handling_opinion, now, existing_audit['id']])
    else:
        task_id = db.execute("""
            INSERT INTO tasks (task_code, paper_id, task_type, assignee_id, status,
                               assigned_at, is_active)
            VALUES (?, ?, 'audit', ?, 'finalized', ?, true) RETURNING id
        """, [f"AU_RESOLVE_{paper_id}_{int(now.timestamp())}", paper_id, user['id'], now]).fetchval()

        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type,
                final_score, handling_opinion)
            VALUES (?, ?, ?, 'audit', ?, ?)
        """, [task_id, paper_id, user['id'], final_score, handling_opinion])

    update_paper_status(db, paper_id, 'finalized')
    db.execute("""
        UPDATE tasks SET status = 'finalized', is_active = false, completed_at = ?
        WHERE paper_id = ? AND task_type = 'audit'
    """, [now, paper_id])
    detect_anomalies(db)
    db.commit()

    return jsonify({
        "message": "差异已处理，试卷已定分",
        "final_score": final_score
    }), 200
