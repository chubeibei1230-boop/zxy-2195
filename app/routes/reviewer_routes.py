from datetime import datetime
from flask import Blueprint, request, jsonify

from app.db import get_db
from app.utils import (
    role_required, get_current_user, row_to_dict, rows_to_list,
    ensure_single_active_task, update_paper_status, STATUS_MAP,
    detect_anomalies, APPEAL_STATUS_MAP, APPEAL_TYPE_MAP,
    RETURN_REASON_TYPE_MAP, RETURN_STATUS_MAP
)

reviewer_bp = Blueprint('reviewer', __name__, url_prefix='/api/reviewer')


@reviewer_bp.route('/dashboard', methods=['GET'])
@role_required('reviewer', 'admin')
def dashboard():
    user = get_current_user()
    db = get_db()
    uid = user['id']

    stats = db.execute("""
        SELECT
            COUNT(CASE WHEN status = 'reviewing' THEN 1 END) as in_progress,
            COUNT(CASE WHEN status = 'pending_assignment' AND assignee_id = ? THEN 1 END) as pending,
            COUNT(CASE WHEN status IN ('finalized', 'pending_audit', 'diff_pending') THEN 1 END) as completed_week,
            COUNT(*) as total
        FROM tasks
        WHERE assignee_id = ? AND task_type = 'review'
          AND created_at >= CURRENT_DATE - INTERVAL 30 DAY
    """, [uid, uid]).fetchone()

    return jsonify({
        "user": user,
        "stats": dict(stats)
    }), 200


@reviewer_bp.route('/tasks', methods=['GET'])
@role_required('reviewer', 'admin')
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
        LEFT JOIN responsibility_groups rg ON t.group_id = rg.id
        LEFT JOIN review_appeals ra ON t.appeal_id = ra.id
        WHERE t.task_type = 'review'
    """
    params = []
    if only_mine:
        base_query += " AND t.assignee_id = ?"
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
               qg.max_score, qg.pass_score, rg.group_name as responsibility_group,
               ra.id as appeal_id, ra.status as appeal_status, ra.appeal_type,
               ra.priority
        {base_query}
        ORDER BY CASE WHEN t.status = 'reviewing' THEN 0
                      WHEN t.status = 'pending_assignment' THEN 1
                      ELSE 2 END, t.assigned_at ASC
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

        review = db.execute("""
            SELECT * FROM reviews WHERE task_id = ? LIMIT 1
        """, [d['id']]).fetchone()
        if review:
            d['review'] = dict(review)

        if d.get('return_record_id'):
            ret = db.execute("""
                SELECT rr.return_code, rr.return_reason, rr.return_reason_type,
                       rr.handling_opinion, rr.return_round, rr.status as return_status,
                       u.real_name as auditor_name
                FROM review_return_records rr
                LEFT JOIN users u ON rr.auditor_id = u.id
                WHERE rr.id = ?
            """, [d['return_record_id']]).fetchone()
            if ret:
                d['return_info'] = {
                    'return_code': ret['return_code'],
                    'return_reason': ret['return_reason'],
                    'return_reason_type': ret['return_reason_type'],
                    'return_reason_type_name': RETURN_REASON_TYPE_MAP.get(ret['return_reason_type'], ret['return_reason_type']),
                    'handling_opinion': ret['handling_opinion'],
                    'return_round': ret['return_round'],
                    'return_status': ret['return_status'],
                    'return_status_name': RETURN_STATUS_MAP.get(ret['return_status'], ret['return_status']),
                    'auditor_name': ret['auditor_name'],
                }

        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@reviewer_bp.route('/tasks/<int:task_id>/start', methods=['POST'])
@role_required('reviewer', 'admin')
def start_task(task_id):
    user = get_current_user()
    db = get_db()

    task = db.execute("""
        SELECT id, paper_id, status, assignee_id, is_active, return_record_id
        FROM tasks WHERE id = ? AND task_type = 'review'
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task['assignee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此任务"}), 403
    if not task['is_active']:
        return jsonify({"error": "任务已失效"}), 400
    if task['status'] == 'reviewing':
        return jsonify({"message": "任务已在阅卷中"}), 200
    if task['status'] not in ('pending_assignment', 'suspended', 'pending_reeval'):
        return jsonify({"error": f"当前状态{task['status']}无法开始"}), 400

    db.execute("""
        UPDATE tasks SET status = 'reviewing', started_at = ? WHERE id = ?
    """, [datetime.now(), task_id])
    update_paper_status(db, task['paper_id'], 'reviewing')
    db.commit()

    return jsonify({"message": "任务已开始阅卷"}), 200


@reviewer_bp.route('/tasks/<int:task_id>/submit', methods=['POST'])
@role_required('reviewer', 'admin')
def submit_review(task_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    initial_score = data.get('initial_score')
    deduction_reason = data.get('deduction_reason', '')
    difficulty_flag = data.get('difficulty_flag', False)
    difficulty_note = data.get('difficulty_note', '')
    completion_note = data.get('completion_note', '')

    if initial_score is None:
        return jsonify({"error": "初评分为必填项"}), 400

    try:
        initial_score = float(initial_score)
    except (ValueError, TypeError):
        return jsonify({"error": "初评分必须为数字"}), 400

    task = db.execute("""
        SELECT t.id, t.paper_id, t.status, t.assignee_id, t.is_active, p.question_group_id
        FROM tasks t
        LEFT JOIN papers p ON t.paper_id = p.id
        WHERE t.id = ? AND t.task_type = 'review'
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task['assignee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此任务"}), 403
    if not task['is_active']:
        return jsonify({"error": "任务已失效"}), 400
    if task['status'] != 'reviewing':
        return jsonify({"error": f"当前状态{task['status']}无法提交"}), 400

    qg = db.execute("SELECT max_score FROM question_groups WHERE id = ?", [task['question_group_id']]).fetchone()
    if qg and (initial_score < 0 or initial_score > float(qg['max_score'])):
        return jsonify({"error": f"初评分应在0到{qg['max_score']}之间"}), 400

    existing_review = db.execute("""
        SELECT id FROM reviews WHERE task_id = ? AND review_type = 'initial'
    """, [task_id]).fetchone()

    now = datetime.now()
    if existing_review:
        db.execute("""
            UPDATE reviews SET
                initial_score = ?, deduction_reason = ?, difficulty_flag = ?,
                difficulty_note = ?, completion_note = ?, updated_at = ?
            WHERE id = ?
        """, [initial_score, deduction_reason, difficulty_flag, difficulty_note, completion_note, now, existing_review['id']])
    else:
        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type,
                initial_score, deduction_reason, difficulty_flag, difficulty_note, completion_note)
            VALUES (?, ?, ?, 'initial', ?, ?, ?, ?, ?)
        """, [task_id, task['paper_id'], user['id'], initial_score, deduction_reason, difficulty_flag, difficulty_note, completion_note])

    db.execute("""
        UPDATE tasks SET status = 'pending_audit', completed_at = ?, is_active = false WHERE id = ?
    """, [now, task_id])
    update_paper_status(db, task['paper_id'], 'pending_audit')

    if task.get('return_record_id') or (hasattr(task, '__getitem__') and task.get('return_record_id')):
        pass

    return_record_id = None
    task_full = db.execute("SELECT return_record_id FROM tasks WHERE id = ?", [task_id]).fetchone()
    if task_full and task_full['return_record_id']:
        return_record_id = task_full['return_record_id']

    if return_record_id:
        db.execute("""
            UPDATE review_return_records
            SET status = 'reevaluated', reevaluated_at = ?, updated_at = ?
            WHERE id = ?
        """, [now, now, return_record_id])

    detect_anomalies(db)
    db.commit()

    return jsonify({
        "message": "初评已提交，等待复核",
        "initial_score": initial_score,
        "is_reeval": return_record_id is not None
    }), 200


@reviewer_bp.route('/tasks/<int:task_id>/save-draft', methods=['POST'])
@role_required('reviewer', 'admin')
def save_draft(task_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    initial_score = data.get('initial_score')
    deduction_reason = data.get('deduction_reason', '')
    difficulty_flag = data.get('difficulty_flag', False)
    difficulty_note = data.get('difficulty_note', '')
    completion_note = data.get('completion_note', '')

    task = db.execute("""
        SELECT id, paper_id, status, assignee_id, is_active
        FROM tasks WHERE id = ? AND task_type = 'review'
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task['assignee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此任务"}), 403
    if not task['is_active']:
        return jsonify({"error": "任务已失效"}), 400

    existing_review = db.execute("""
        SELECT id FROM reviews WHERE task_id = ? AND review_type = 'initial'
    """, [task_id]).fetchone()

    now = datetime.now()
    if existing_review:
        db.execute("""
            UPDATE reviews SET
                initial_score = ?, deduction_reason = ?, difficulty_flag = ?,
                difficulty_note = ?, completion_note = ?, updated_at = ?
            WHERE id = ?
        """, [initial_score, deduction_reason, difficulty_flag, difficulty_note, completion_note, now, existing_review['id']])
    else:
        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type,
                initial_score, deduction_reason, difficulty_flag, difficulty_note, completion_note)
            VALUES (?, ?, ?, 'initial', ?, ?, ?, ?, ?)
        """, [task_id, task['paper_id'], user['id'], initial_score, deduction_reason, difficulty_flag, difficulty_note, completion_note])
    db.commit()

    return jsonify({"message": "草稿已保存"}), 200


@reviewer_bp.route('/tasks/<int:task_id>/return', methods=['POST'])
@role_required('reviewer', 'admin')
def return_task(task_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    return_reason = data.get('return_reason', '')
    if not return_reason:
        return jsonify({"error": "退回原因必填"}), 400

    task = db.execute("""
        SELECT id, paper_id, status, assignee_id, is_active
        FROM tasks WHERE id = ? AND task_type = 'review'
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task['assignee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此任务"}), 403
    if not task['is_active']:
        return jsonify({"error": "任务已失效"}), 400
    if task['status'] not in ('reviewing', 'pending_assignment'):
        return jsonify({"error": f"当前状态{task['status']}无法退回"}), 400

    db.execute("""
        UPDATE tasks SET status = 'returned', return_reason = ?, is_active = false WHERE id = ?
    """, [return_reason, task_id])
    update_paper_status(db, task['paper_id'], 'pending_assignment')
    db.commit()

    return jsonify({"message": "任务已退回", "return_reason": return_reason}), 200


@reviewer_bp.route('/papers/<int:paper_id>/review-history', methods=['GET'])
@role_required('reviewer', 'admin')
def review_history(paper_id):
    db = get_db()
    rows = db.execute("""
        SELECT r.*, t.task_code, t.status as task_status, t.review_round,
               u.real_name as reviewer_name, u.username
        FROM reviews r
        LEFT JOIN tasks t ON r.task_id = t.id
        LEFT JOIN users u ON r.reviewer_id = u.id
        WHERE r.paper_id = ?
        ORDER BY r.created_at DESC
    """, [paper_id]).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['task_status_name'] = STATUS_MAP.get(d['task_status'], d['task_status'])
        result.append(d)

    returns = db.execute("""
        SELECT rr.*, u.real_name as auditor_name
        FROM review_return_records rr
        LEFT JOIN users u ON rr.auditor_id = u.id
        WHERE rr.paper_id = ?
        ORDER BY rr.created_at ASC
    """, [paper_id]).fetchall()

    return_history = []
    for rr in returns:
        d = dict(rr)
        d['return_reason_type_name'] = RETURN_REASON_TYPE_MAP.get(d['return_reason_type'], d['return_reason_type'])
        d['status_name'] = RETURN_STATUS_MAP.get(d['status'], d['status'])
        return_history.append(d)

    paper = db.execute("""
        SELECT current_round, return_count, latest_return_reason,
               latest_return_reason_type, current_status
        FROM papers WHERE id = ?
    """, [paper_id]).fetchone()

    paper_info = dict(paper) if paper else {}
    if paper_info.get('current_status'):
        paper_info['status_name'] = STATUS_MAP.get(paper_info['current_status'], paper_info['current_status'])
    if paper_info.get('latest_return_reason_type'):
        paper_info['latest_return_reason_type_name'] = RETURN_REASON_TYPE_MAP.get(
            paper_info['latest_return_reason_type'], paper_info['latest_return_reason_type'])

    return jsonify({
        "paper": paper_info,
        "reviews": result,
        "return_history": return_history
    }), 200
