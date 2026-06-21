from datetime import datetime
from flask import Blueprint, request, jsonify

from app.db import get_db
from app.utils import (
    role_required, get_current_user, generate_appeal_code,
    add_appeal_log, get_paper_latest_appeal,
    APPEAL_STATUS_MAP, APPEAL_TYPE_MAP, APPEAL_PRIORITY_MAP,
    STATUS_MAP, ROLE_MAP, update_paper_status, ensure_single_active_task,
    generate_task_code, calculate_deadline
)
from config import Config

appeal_bp = Blueprint('appeal', __name__, url_prefix='/api/appeals')


@appeal_bp.route('', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def list_appeals():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    status = request.args.get('status')
    appeal_type = request.args.get('appeal_type')
    priority = request.args.get('priority')
    paper_id = request.args.get('paper_id', type=int)
    applicant_id = request.args.get('applicant_id', type=int)
    batch_id = request.args.get('batch_id', type=int)
    keyword = request.args.get('keyword', '').strip()

    base_query = """
        FROM review_appeals ra
        LEFT JOIN papers p ON ra.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users u ON ra.applicant_id = u.id
        LEFT JOIN users h ON ra.handler_id = h.id
        WHERE 1=1
    """
    params = []

    if status:
        base_query += " AND ra.status = ?"
        params.append(status)
    if appeal_type:
        base_query += " AND ra.appeal_type = ?"
        params.append(appeal_type)
    if priority:
        base_query += " AND ra.priority = ?"
        params.append(priority)
    if paper_id:
        base_query += " AND ra.paper_id = ?"
        params.append(paper_id)
    if applicant_id:
        base_query += " AND ra.applicant_id = ?"
        params.append(applicant_id)
    if batch_id:
        base_query += " AND p.batch_id = ?"
        params.append(batch_id)
    if keyword:
        base_query += " AND (p.paper_number LIKE ? OR p.candidate_name LIKE ? OR ra.reason LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT ra.*, p.paper_number, p.candidate_name, p.current_status as paper_status,
               p.is_reviewing, p.appeal_count,
               b.batch_name, b.batch_code, qg.group_name as question_group_name,
               u.real_name as applicant_name, u.username as applicant_username,
               h.real_name as handler_name, h.username as handler_username
        {base_query}
        ORDER BY 
            CASE ra.priority
                WHEN 'high' THEN 0
                WHEN 'medium' THEN 1
                ELSE 2
            END,
            ra.id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = APPEAL_STATUS_MAP.get(d['status'], d['status'])
        d['appeal_type_name'] = APPEAL_TYPE_MAP.get(d['appeal_type'], d['appeal_type'])
        d['priority_name'] = APPEAL_PRIORITY_MAP.get(d['priority'], d['priority'])
        d['paper_status_name'] = STATUS_MAP.get(d['paper_status'], d['paper_status'])
        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@appeal_bp.route('/<int:appeal_id>', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def get_appeal_detail(appeal_id):
    db = get_db()

    appeal = db.execute("""
        SELECT ra.*, p.paper_number, p.candidate_name, p.candidate_id,
               p.current_status as paper_status, p.is_reviewing, p.appeal_count,
               p.paper_content, p.storage_path,
               b.batch_name, b.batch_code, qg.group_name as question_group_name,
               qg.max_score, qg.pass_score,
               u.real_name as applicant_name, u.username as applicant_username,
               h.real_name as handler_name, h.username as handler_username
        FROM review_appeals ra
        LEFT JOIN papers p ON ra.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users u ON ra.applicant_id = u.id
        LEFT JOIN users h ON ra.handler_id = h.id
        WHERE ra.id = ?
    """, [appeal_id]).fetchone()

    if not appeal:
        return jsonify({"error": "复评申请不存在"}), 404

    result = dict(appeal)
    result['status_name'] = APPEAL_STATUS_MAP.get(result['status'], result['status'])
    result['appeal_type_name'] = APPEAL_TYPE_MAP.get(result['appeal_type'], result['appeal_type'])
    result['priority_name'] = APPEAL_PRIORITY_MAP.get(result['priority'], result['priority'])
    result['paper_status_name'] = STATUS_MAP.get(result['paper_status'], result['paper_status'])

    logs = db.execute("""
        SELECT l.*, u.real_name as operator_name, u.username as operator_username
        FROM review_appeal_logs l
        LEFT JOIN users u ON l.operator_id = u.id
        WHERE l.appeal_id = ?
        ORDER BY l.id ASC
    """, [appeal_id]).fetchall()

    log_list = []
    for log in logs:
        d = dict(log)
        if d.get('from_status'):
            d['from_status_name'] = APPEAL_STATUS_MAP.get(d['from_status'], d['from_status'])
        if d.get('to_status'):
            d['to_status_name'] = APPEAL_STATUS_MAP.get(d['to_status'], d['to_status'])
        log_list.append(d)

    result['logs'] = log_list

    init_review = db.execute("""
        SELECT r.*, u.real_name as reviewer_name, u.username as reviewer_username,
               t.task_code, t.status as task_status, t.review_round
        FROM reviews r
        LEFT JOIN users u ON r.reviewer_id = u.id
        LEFT JOIN tasks t ON r.task_id = t.id
        WHERE r.paper_id = ? AND r.review_type = 'initial' AND r.review_round = 1
        ORDER BY r.id DESC LIMIT 1
    """, [appeal['paper_id']]).fetchone()
    if init_review:
        result['initial_review'] = dict(init_review)

    audit_review = db.execute("""
        SELECT r.*, u.real_name as reviewer_name, u.username as reviewer_username,
               t.task_code, t.status as task_status, t.review_round
        FROM reviews r
        LEFT JOIN users u ON r.reviewer_id = u.id
        LEFT JOIN tasks t ON r.task_id = t.id
        WHERE r.paper_id = ? AND r.review_type = 'audit' AND r.review_round = 1
        ORDER BY r.id DESC LIMIT 1
    """, [appeal['paper_id']]).fetchone()
    if audit_review:
        result['audit_review'] = dict(audit_review)

    review_tasks = db.execute("""
        SELECT t.*, u.real_name as assignee_name, u.username as assignee_username
        FROM tasks t
        LEFT JOIN users u ON t.assignee_id = u.id
        WHERE t.paper_id = ? AND t.is_review_task = true
        ORDER BY t.id DESC
    """, [appeal['paper_id']]).fetchall()

    review_task_list = []
    for t in review_tasks:
        d = dict(t)
        d['status_name'] = STATUS_MAP.get(d['status'], d['status'])
        review_task_list.append(d)
    result['review_tasks'] = review_task_list

    return jsonify(result), 200


@appeal_bp.route('', methods=['POST'])
@role_required('admin')
def create_appeal():
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    paper_id = data.get('paper_id')
    appeal_type = data.get('appeal_type')
    priority = data.get('priority', 'medium')
    reason = data.get('reason', '').strip()
    description = data.get('description', '')

    if not paper_id or not appeal_type or not reason:
        return jsonify({"error": "试卷ID、申请类型、申请原因为必填项"}), 400

    if appeal_type not in ('quality_check', 'abnormal_diff', 'manual_correction'):
        return jsonify({"error": "无效的申请类型"}), 400
    if priority not in ('high', 'medium', 'low'):
        return jsonify({"error": "无效的优先级"}), 400

    paper = db.execute("""
        SELECT id, current_status, is_reviewing, current_appeal_id, appeal_count
        FROM papers WHERE id = ?
    """, [paper_id]).fetchone()

    if not paper:
        return jsonify({"error": "试卷不存在"}), 404

    if paper['current_status'] not in ('pending_audit', 'diff_pending', 'finalized'):
        return jsonify({"error": "试卷当前状态不允许发起复评申请，仅待复核、差异待处理、已定分状态可申请"}), 400

    if paper['is_reviewing']:
        return jsonify({"error": "该试卷已有进行中的复评申请"}), 400

    appeal_code = generate_appeal_code()
    now = datetime.now()

    cursor = db.execute("""
        INSERT INTO review_appeals (
            appeal_code, paper_id, applicant_id, appeal_type, priority,
            reason, description, status, original_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        RETURNING id
    """, [appeal_code, paper_id, user['id'], appeal_type, priority, reason, description, paper['current_status']])
    appeal_id = cursor.fetchval()

    add_appeal_log(db, appeal_id, user['id'], 'create', '创建复评申请', None, 'pending')

    db.execute("""
        UPDATE papers SET is_reviewing = true, current_appeal_id = ?, appeal_count = appeal_count + 1
        WHERE id = ?
    """, [appeal_id, paper_id])

    db.commit()

    return jsonify({
        "id": appeal_id,
        "appeal_code": appeal_code,
        "message": "复评申请创建成功"
    }), 201


@appeal_bp.route('/<int:appeal_id>/accept', methods=['POST'])
@role_required('admin')
def accept_appeal(appeal_id):
    data = request.get_json() or {}
    user = get_current_user()
    db = get_db()
    remark = data.get('remark', '')

    appeal = db.execute("""
        SELECT id, status, paper_id FROM review_appeals WHERE id = ?
    """, [appeal_id]).fetchone()

    if not appeal:
        return jsonify({"error": "复评申请不存在"}), 404

    if appeal['status'] != 'pending':
        return jsonify({"error": "只有申请中状态的申请可以受理"}), 400

    now = datetime.now()
    db.execute("""
        UPDATE review_appeals SET status = 'accepted', handler_id = ?, accepted_at = ?, updated_at = ?
        WHERE id = ?
    """, [user['id'], now, now, appeal_id])

    add_appeal_log(db, appeal_id, user['id'], 'accept', remark, 'pending', 'accepted')
    db.commit()

    return jsonify({"message": "复评申请已受理"}), 200


@appeal_bp.route('/<int:appeal_id>/start', methods=['POST'])
@role_required('admin')
def start_review(appeal_id):
    data = request.get_json() or {}
    user = get_current_user()
    db = get_db()
    remark = data.get('remark', '')
    assignee_id = data.get('assignee_id')
    task_type = data.get('task_type', 'audit')
    group_id = data.get('group_id')

    appeal = db.execute("""
        SELECT id, status, paper_id, priority, original_status
        FROM review_appeals WHERE id = ?
    """, [appeal_id]).fetchone()

    if not appeal:
        return jsonify({"error": "复评申请不存在"}), 404

    if appeal['status'] not in ('accepted', 'reviewing'):
        return jsonify({"error": "只有已受理或复评中状态的申请可以开始复评"}), 400

    paper = db.execute("""
        SELECT id, question_group_id, batch_id FROM papers WHERE id = ?
    """, [appeal['paper_id']]).fetchone()

    now = datetime.now()

    if not assignee_id:
        assignee_id = user['id']

    assignee = db.execute("SELECT id, role, group_id FROM users WHERE id = ? AND is_active = true", [assignee_id]).fetchone()
    if not assignee:
        return jsonify({"error": "处理人不存在或已禁用"}), 400

    if task_type == 'review' and assignee['role'] not in ('reviewer', 'admin'):
        return jsonify({"error": "复评任务只能分配给阅卷员或管理员"}), 400
    if task_type == 'audit' and assignee['role'] not in ('auditor', 'admin'):
        return jsonify({"error": "复核任务只能分配给复核员或管理员"}), 400

    active_task = ensure_single_active_task(db, appeal['paper_id'], task_type)
    if active_task:
        db.execute("UPDATE tasks SET is_active = false WHERE id = ?", [active_task['id']])

    appeal_count = db.execute("SELECT appeal_count FROM papers WHERE id = ?", [appeal['paper_id']]).fetchval()
    review_round = (appeal_count or 0) + 1

    rg = None
    final_group_id = group_id
    if not final_group_id:
        rg = db.execute("""
            SELECT id, review_time_limit_hours, audit_time_limit_hours
            FROM responsibility_groups
            WHERE question_group_id = ? AND (batch_id = ? OR batch_id IS NULL)
            ORDER BY batch_id DESC NULLS LAST LIMIT 1
        """, [paper['question_group_id'], paper['batch_id']]).fetchone()
        if rg:
            final_group_id = rg['id']

    if rg:
        deadline_hours = (
            float(rg['review_time_limit_hours']) if task_type == 'review'
            else float(rg['audit_time_limit_hours'])
        )
    else:
        deadline_hours = Config.REVIEW_TIMEOUT_HOURS if task_type == 'review' else 24
    deadline = calculate_deadline(deadline_hours)

    new_status = 'reviewing' if task_type == 'review' else 'pending_audit'
    task_code = generate_task_code(task_type)

    task_cursor = db.execute("""
        INSERT INTO tasks (
            task_code, paper_id, task_type, assignee_id, group_id, status,
            assigned_at, deadline_at, is_active, is_review_task, appeal_id, review_round
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, true, true, ?, ?)
        RETURNING id
    """, [task_code, appeal['paper_id'], task_type, assignee_id, final_group_id, new_status,
          now, deadline, appeal_id, review_round])
    task_id = task_cursor.fetchval()

    if task_type == 'review':
        db.execute("""
            INSERT INTO reviews (
                task_id, paper_id, reviewer_id, review_type, is_review, appeal_id, review_round
            ) VALUES (?, ?, ?, 'initial', true, ?, ?)
        """, [task_id, appeal['paper_id'], assignee_id, appeal_id, review_round])
    else:
        db.execute("""
            INSERT INTO reviews (
                task_id, paper_id, reviewer_id, review_type, is_review, appeal_id, review_round
            ) VALUES (?, ?, ?, 'audit', true, ?, ?)
        """, [task_id, appeal['paper_id'], assignee_id, appeal_id, review_round])

    update_paper_status(db, appeal['paper_id'], 'reviewing')

    db.execute("""
        UPDATE review_appeals SET status = 'reviewing', handler_id = ?, started_at = ?, updated_at = ?
        WHERE id = ?
    """, [user['id'], now, now, appeal_id])

    add_appeal_log(db, appeal_id, user['id'], 'start',
                   remark if remark else f"分配{task_type}任务给{assignee.get('real_name', assignee['username'])}",
                   appeal['status'], 'reviewing')

    db.commit()

    return jsonify({
        "message": "复评已开始，任务已分配",
        "task_id": task_id,
        "task_code": task_code
    }), 200


@appeal_bp.route('/<int:appeal_id>/complete', methods=['POST'])
@role_required('admin')
def complete_appeal(appeal_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    conclusion = data.get('conclusion', '').strip()
    final_score = data.get('final_score')

    if not conclusion:
        return jsonify({"error": "复评结论为必填项"}), 400

    appeal = db.execute("""
        SELECT id, status, paper_id FROM review_appeals WHERE id = ?
    """, [appeal_id]).fetchone()

    if not appeal:
        return jsonify({"error": "复评申请不存在"}), 404

    if appeal['status'] != 'reviewing':
        return jsonify({"error": "只有复评中状态的申请可以完成"}), 400

    now = datetime.now()

    if final_score is not None:
        try:
            final_score = float(final_score)
        except (ValueError, TypeError):
            return jsonify({"error": "最终分必须为数字"}), 400

    db.execute("""
        UPDATE review_appeals
        SET status = 'completed', conclusion = ?, final_score = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
    """, [conclusion, final_score, now, now, appeal_id])

    add_appeal_log(db, appeal_id, user['id'], 'complete', conclusion, 'reviewing', 'completed')

    db.execute("""
        UPDATE papers SET is_reviewing = false, current_appeal_id = NULL
        WHERE id = ?
    """, [appeal['paper_id']])

    db.execute("""
        UPDATE tasks SET is_active = false WHERE paper_id = ? AND is_review_task = true AND is_active = true
    """, [appeal['paper_id']])

    if final_score is not None:
        latest_audit = db.execute("""
            SELECT id FROM reviews
            WHERE paper_id = ? AND review_type = 'audit' AND is_review = true
            ORDER BY id DESC LIMIT 1
        """, [appeal['paper_id']]).fetchone()
        if latest_audit:
            db.execute("""
                UPDATE reviews SET final_score = ?, updated_at = ? WHERE id = ?
            """, [final_score, now, latest_audit['id']])

    update_paper_status(db, appeal['paper_id'], 'finalized')

    db.commit()

    return jsonify({"message": "复评申请已完成"}), 200


@appeal_bp.route('/<int:appeal_id>/reject', methods=['POST'])
@role_required('admin')
def reject_appeal(appeal_id):
    data = request.get_json()
    user = get_current_user()
    db = get_db()

    reason = data.get('reason', '').strip()
    if not reason:
        return jsonify({"error": "驳回原因为必填项"}), 400

    appeal = db.execute("""
        SELECT id, status, paper_id, original_status FROM review_appeals WHERE id = ?
    """, [appeal_id]).fetchone()

    if not appeal:
        return jsonify({"error": "复评申请不存在"}), 404

    if appeal['status'] not in ('pending', 'accepted'):
        return jsonify({"error": "只有申请中或已受理状态的申请可以驳回"}), 400

    now = datetime.now()

    db.execute("""
        UPDATE review_appeals
        SET status = 'rejected', conclusion = ?, handler_id = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
    """, [reason, user['id'], now, now, appeal_id])

    add_appeal_log(db, appeal_id, user['id'], 'reject', reason, appeal['status'], 'rejected')

    db.execute("""
        UPDATE papers SET is_reviewing = false, current_appeal_id = NULL
        WHERE id = ?
    """, [appeal['paper_id']])

    db.execute("""
        UPDATE tasks SET is_active = false WHERE paper_id = ? AND is_review_task = true AND is_active = true
    """, [appeal['paper_id']])

    if appeal['original_status']:
        update_paper_status(db, appeal['paper_id'], appeal['original_status'])

    db.commit()

    return jsonify({"message": "复评申请已驳回"}), 200


@appeal_bp.route('/paper/<int:paper_id>', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def get_paper_appeals(paper_id):
    db = get_db()

    rows = db.execute("""
        SELECT ra.*, u.real_name as applicant_name, u.username as applicant_username,
               h.real_name as handler_name, h.username as handler_username
        FROM review_appeals ra
        LEFT JOIN users u ON ra.applicant_id = u.id
        LEFT JOIN users h ON ra.handler_id = h.id
        WHERE ra.paper_id = ?
        ORDER BY ra.id DESC
    """, [paper_id]).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = APPEAL_STATUS_MAP.get(d['status'], d['status'])
        d['appeal_type_name'] = APPEAL_TYPE_MAP.get(d['appeal_type'], d['appeal_type'])
        d['priority_name'] = APPEAL_PRIORITY_MAP.get(d['priority'], d['priority'])
        result.append(d)

    return jsonify(result), 200


@appeal_bp.route('/summary', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def appeal_summary():
    db = get_db()

    by_status = db.execute("""
        SELECT status, COUNT(*) as count
        FROM review_appeals
        GROUP BY status
        ORDER BY status
    """).fetchall()

    by_type = db.execute("""
        SELECT appeal_type, COUNT(*) as count
        FROM review_appeals
        GROUP BY appeal_type
        ORDER BY appeal_type
    """).fetchall()

    total = db.execute("SELECT COUNT(*) as total FROM review_appeals").fetchval()
    pending_count = db.execute("SELECT COUNT(*) as cnt FROM review_appeals WHERE status = 'pending'").fetchval()
    reviewing_count = db.execute("SELECT COUNT(*) as cnt FROM review_appeals WHERE status = 'reviewing'").fetchval()
    completed_count = db.execute("SELECT COUNT(*) as cnt FROM review_appeals WHERE status = 'completed'").fetchval()
    rejected_count = db.execute("SELECT COUNT(*) as cnt FROM review_appeals WHERE status = 'rejected'").fetchval()

    status_list = []
    for s in by_status:
        d = dict(s)
        d['status_name'] = APPEAL_STATUS_MAP.get(d['status'], d['status'])
        status_list.append(d)

    type_list = []
    for t in by_type:
        d = dict(t)
        d['appeal_type_name'] = APPEAL_TYPE_MAP.get(d['appeal_type'], d['appeal_type'])
        type_list.append(d)

    return jsonify({
        "total": total,
        "pending_count": pending_count,
        "reviewing_count": reviewing_count,
        "completed_count": completed_count,
        "rejected_count": rejected_count,
        "by_status": status_list,
        "by_type": type_list
    }), 200
