from datetime import datetime
from flask import Blueprint, request, jsonify

from app.db import get_db
from app.utils import (
    role_required, get_current_user, rows_to_list,
    STATUS_MAP, ROLE_MAP, SUPERVISION_STATUS_MAP, SUPERVISION_URGENCY_MAP,
    generate_supervision_code, add_supervision_log,
    get_task_supervision_info, get_paper_supervision_info
)

supervision_bp = Blueprint('supervisions', __name__, url_prefix='/api/supervisions')


@supervision_bp.route('', methods=['POST'])
@role_required('admin')
def create_supervision():
    data = request.get_json(force=True, silent=True) or {}
    user = get_current_user()
    db = get_db()

    task_id = data.get('task_id')
    paper_id = data.get('paper_id')
    supervisee_id = data.get('supervisee_id')
    reason = (data.get('reason') or '').strip()
    urgency_level = data.get('urgency_level', 'normal')
    requirements = (data.get('requirements') or '').strip()
    expected_complete_at = data.get('expected_complete_at')

    if not task_id or not reason or not supervisee_id:
        return jsonify({"error": "任务ID、被督办人、督办原因为必填项"}), 400

    if urgency_level not in SUPERVISION_URGENCY_MAP:
        return jsonify({"error": "无效的紧急等级"}), 400

    task = db.execute("""
        SELECT id, paper_id, task_type, status, assignee_id, is_active
        FROM tasks WHERE id = ?
    """, [task_id]).fetchone()

    if not task:
        return jsonify({"error": "任务不存在"}), 404

    if not task['is_active']:
        return jsonify({"error": "任务已失效，无法督办"}), 400

    if task['status'] not in ('reviewing', 'pending_audit', 'diff_pending', 'pending_reeval'):
        return jsonify({"error": f"任务当前状态{task['status']}不在进行中环节，无法督办"}), 400

    supervisee = db.execute(
        "SELECT id, role, is_active FROM users WHERE id = ?", [supervisee_id]
    ).fetchone()

    if not supervisee or not supervisee['is_active']:
        return jsonify({"error": "被督办人不存在或已禁用"}), 400

    if task['task_type'] == 'review' and supervisee['role'] not in ('reviewer', 'admin'):
        return jsonify({"error": "阅卷任务的被督办人应为阅卷员或管理员"}), 400
    if task['task_type'] == 'audit' and supervisee['role'] not in ('auditor', 'admin'):
        return jsonify({"error": "复核任务的被督办人应为复核员或管理员"}), 400

    final_paper_id = paper_id or task['paper_id']

    active_supervision = db.execute("""
        SELECT id FROM task_supervisions
        WHERE task_id = ? AND status IN ('pending', 'acknowledged', 'processing')
    """, [task_id]).fetchone()

    if active_supervision:
        return jsonify({"error": "该任务已有进行中的督办，请先处理或关闭现有督办"}), 400

    supervision_code = generate_supervision_code()
    now = datetime.now()

    cursor = db.execute("""
        INSERT INTO task_supervisions (
            supervision_code, task_id, paper_id, supervisor_id, supervisee_id,
            reason, urgency_level, requirements, expected_complete_at, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        RETURNING id
    """, [supervision_code, task_id, final_paper_id, user['id'], supervisee_id,
          reason, urgency_level, requirements, expected_complete_at, now, now])
    supervision_id = cursor.fetchval()

    add_supervision_log(db, supervision_id, user['id'], 'create', '发起督办', None, 'pending')

    paper = db.execute("SELECT paper_number FROM papers WHERE id = ?", [final_paper_id]).fetchone()
    paper_number = paper['paper_number'] if paper else str(final_paper_id)

    alert_level = 'critical' if urgency_level == 'critical' else ('warning' if urgency_level == 'urgent' else 'info')
    urgency_name = SUPERVISION_URGENCY_MAP.get(urgency_level, urgency_level)
    try:
        db.execute("""
            INSERT INTO alerts (alert_type, alert_level, paper_id, task_id, supervision_id, message, detail_json)
            VALUES ('task_supervision', ?, ?, ?, ?, ?, ?)
        """, [
            alert_level, final_paper_id, task_id, supervision_id,
            f"任务督办：试卷{paper_number}，紧急等级：{urgency_name}，原因：{reason[:50]}",
            f'{{"supervision_id": {supervision_id}, "supervision_code": "{supervision_code}", "urgency_level": "{urgency_level}"}}'
        ])
    except Exception:
        db.execute("""
            INSERT INTO alerts (alert_type, alert_level, paper_id, task_id, message, detail_json)
            VALUES ('task_supervision', ?, ?, ?, ?, ?)
        """, [
            alert_level, final_paper_id, task_id,
            f"任务督办：试卷{paper_number}，紧急等级：{urgency_name}，原因：{reason[:50]}",
            f'{{"supervision_id": {supervision_id}, "supervision_code": "{supervision_code}", "urgency_level": "{urgency_level}"}}'
        ])

    db.commit()

    return jsonify({
        "id": supervision_id,
        "supervision_code": supervision_code,
        "message": "督办已发起"
    }), 201


@supervision_bp.route('', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def list_supervisions():
    db = get_db()
    user = get_current_user()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    status = request.args.get('status')
    urgency_level = request.args.get('urgency_level')
    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    supervisee_id = request.args.get('supervisee_id', type=int)
    supervisor_id = request.args.get('supervisor_id', type=int)
    task_type = request.args.get('task_type')

    base_query = """
        FROM task_supervisions ts
        LEFT JOIN tasks t ON ts.task_id = t.id
        LEFT JOIN papers p ON ts.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users su ON ts.supervisor_id = su.id
        LEFT JOIN users se ON ts.supervisee_id = se.id
        WHERE 1=1
    """
    params = []

    if user['role'] != 'admin':
        base_query += " AND (ts.supervisee_id = ? OR ts.supervisor_id = ?)"
        params.extend([user['id'], user['id']])

    if status:
        base_query += " AND ts.status = ?"
        params.append(status)
    if urgency_level:
        base_query += " AND ts.urgency_level = ?"
        params.append(urgency_level)
    if batch_id:
        base_query += " AND p.batch_id = ?"
        params.append(batch_id)
    if question_group_id:
        base_query += " AND p.question_group_id = ?"
        params.append(question_group_id)
    if supervisee_id:
        base_query += " AND ts.supervisee_id = ?"
        params.append(supervisee_id)
    if supervisor_id:
        base_query += " AND ts.supervisor_id = ?"
        params.append(supervisor_id)
    if task_type:
        base_query += " AND t.task_type = ?"
        params.append(task_type)

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT ts.*, t.task_code, t.task_type, t.status as task_status,
               p.paper_number, p.candidate_name, p.current_status as paper_status,
               b.batch_name, b.batch_code, qg.group_name as question_group_name,
               su.real_name as supervisor_name, su.username as supervisor_username,
               se.real_name as supervisee_name, se.username as supervisee_username
        {base_query}
        ORDER BY
            CASE ts.urgency_level
                WHEN 'critical' THEN 0
                WHEN 'urgent' THEN 1
                ELSE 2
            END,
            CASE ts.status
                WHEN 'pending' THEN 0
                WHEN 'processing' THEN 1
                WHEN 'acknowledged' THEN 2
                ELSE 3
            END,
            ts.created_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = SUPERVISION_STATUS_MAP.get(d['status'], d['status'])
        d['urgency_level_name'] = SUPERVISION_URGENCY_MAP.get(d['urgency_level'], d['urgency_level'])
        d['task_status_name'] = STATUS_MAP.get(d['task_status'], d['task_status']) if d.get('task_status') else None
        d['paper_status_name'] = STATUS_MAP.get(d['paper_status'], d['paper_status']) if d.get('paper_status') else None

        reassignments = db.execute("""
            SELECT tr.*, u1.real_name as from_user_name, u1.username as from_user_username,
                   u2.real_name as to_user_name, u2.username as to_user_username
            FROM task_reassignments tr
            LEFT JOIN users u1 ON tr.from_user_id = u1.id
            LEFT JOIN users u2 ON tr.to_user_id = u2.id
            WHERE tr.supervision_id = ?
            ORDER BY tr.id ASC
        """, [d['id']]).fetchall()
        d['reassignments'] = [dict(ra) for ra in reassignments]

        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@supervision_bp.route('/<int:supervision_id>', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def get_supervision_detail(supervision_id):
    db = get_db()
    user = get_current_user()

    supervision = db.execute("""
        SELECT ts.*, t.task_code, t.task_type, t.status as task_status, t.assignee_id,
               p.paper_number, p.candidate_name, p.candidate_id, p.current_status as paper_status,
               b.batch_name, b.batch_code, qg.group_name as question_group_name,
               qg.max_score, qg.pass_score,
               su.real_name as supervisor_name, su.username as supervisor_username,
               se.real_name as supervisee_name, se.username as supervisee_username
        FROM task_supervisions ts
        LEFT JOIN tasks t ON ts.task_id = t.id
        LEFT JOIN papers p ON ts.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users su ON ts.supervisor_id = su.id
        LEFT JOIN users se ON ts.supervisee_id = se.id
        WHERE ts.id = ?
    """, [supervision_id]).fetchone()

    if not supervision:
        return jsonify({"error": "督办记录不存在"}), 404

    result = dict(supervision)
    result['status_name'] = SUPERVISION_STATUS_MAP.get(result['status'], result['status'])
    result['urgency_level_name'] = SUPERVISION_URGENCY_MAP.get(result['urgency_level'], result['urgency_level'])
    result['task_status_name'] = STATUS_MAP.get(result['task_status'], result['task_status']) if result.get('task_status') else None
    result['paper_status_name'] = STATUS_MAP.get(result['paper_status'], result['paper_status']) if result.get('paper_status') else None

    logs = db.execute("""
        SELECT sl.*, u.real_name as operator_name, u.username as operator_username
        FROM supervision_logs sl
        LEFT JOIN users u ON sl.operator_id = u.id
        WHERE sl.supervision_id = ?
        ORDER BY sl.id ASC
    """, [supervision_id]).fetchall()

    log_list = []
    for log in logs:
        d = dict(log)
        if d.get('from_status'):
            d['from_status_name'] = SUPERVISION_STATUS_MAP.get(d['from_status'], d['from_status'])
        if d.get('to_status'):
            d['to_status_name'] = SUPERVISION_STATUS_MAP.get(d['to_status'], d['to_status'])
        log_list.append(d)
    result['logs'] = log_list

    reassignments = db.execute("""
        SELECT tr.*, u1.real_name as from_user_name, u1.username as from_user_username,
               u2.real_name as to_user_name, u2.username as to_user_username
        FROM task_reassignments tr
        LEFT JOIN users u1 ON tr.from_user_id = u1.id
        LEFT JOIN users u2 ON tr.to_user_id = u2.id
        WHERE tr.supervision_id = ?
        ORDER BY tr.id ASC
    """, [supervision_id]).fetchall()
    result['reassignments'] = [dict(ra) for ra in reassignments]

    return jsonify(result), 200


@supervision_bp.route('/<int:supervision_id>', methods=['PUT'])
@role_required('admin')
def update_supervision(supervision_id):
    data = request.get_json(force=True, silent=True) or {}
    user = get_current_user()
    db = get_db()

    supervision = db.execute("SELECT id, status FROM task_supervisions WHERE id = ?", [supervision_id]).fetchone()
    if not supervision:
        return jsonify({"error": "督办记录不存在"}), 404

    if supervision['status'] not in ('pending', 'acknowledged', 'processing'):
        return jsonify({"error": "当前督办状态不允许修改"}), 400

    fields = []
    params = []
    for k in ('reason', 'urgency_level', 'requirements', 'expected_complete_at'):
        if k in data:
            if k == 'urgency_level' and data[k] not in SUPERVISION_URGENCY_MAP:
                continue
            fields.append(f"{k} = ?")
            params.append(data[k])

    if fields:
        fields.append("updated_at = ?")
        params.append(datetime.now())
        params.append(supervision_id)
        db.execute(f"UPDATE task_supervisions SET {', '.join(fields)} WHERE id = ?", params)

        add_supervision_log(db, supervision_id, user['id'], 'update', '更新督办信息')

    db.commit()
    return jsonify({"message": "督办信息已更新"}), 200


@supervision_bp.route('/<int:supervision_id>/close', methods=['POST'])
@role_required('admin')
def close_supervision(supervision_id):
    data = request.get_json(force=True, silent=True) or {}
    user = get_current_user()
    db = get_db()

    remark = (data.get('remark') or '').strip()

    supervision = db.execute("SELECT id, status FROM task_supervisions WHERE id = ?", [supervision_id]).fetchone()
    if not supervision:
        return jsonify({"error": "督办记录不存在"}), 404

    if supervision['status'] == 'closed':
        return jsonify({"error": "督办已关闭"}), 400

    now = datetime.now()
    db.execute("""
        UPDATE task_supervisions SET status = 'closed', closed_at = ?, updated_at = ?
        WHERE id = ?
    """, [now, now, supervision_id])

    add_supervision_log(db, supervision_id, user['id'], 'close', remark or '关闭督办', supervision['status'], 'closed')
    db.commit()

    return jsonify({"message": "督办已关闭"}), 200


@supervision_bp.route('/<int:supervision_id>/reassign', methods=['POST'])
@role_required('admin')
def reassign_task(supervision_id):
    data = request.get_json(force=True, silent=True) or {}
    user = get_current_user()
    db = get_db()

    to_user_id = data.get('to_user_id')
    reason = (data.get('reason') or '').strip()

    if not to_user_id:
        return jsonify({"error": "转派目标人员为必填项"}), 400

    supervision = db.execute("""
        SELECT id, task_id, paper_id, supervisee_id, status FROM task_supervisions WHERE id = ?
    """, [supervision_id]).fetchone()

    if not supervision:
        return jsonify({"error": "督办记录不存在"}), 404

    if supervision['status'] in ('closed', 'completed'):
        return jsonify({"error": "已关闭或已完成的督办不允许转派"}), 400

    task = db.execute("""
        SELECT id, task_type, status, assignee_id, is_active, paper_id
        FROM tasks WHERE id = ?
    """, [supervision['task_id']]).fetchone()

    if not task:
        return jsonify({"error": "关联任务不存在"}), 404

    to_user = db.execute(
        "SELECT id, role, is_active, real_name, username FROM users WHERE id = ? AND is_active = true",
        [to_user_id]
    ).fetchone()

    if not to_user:
        return jsonify({"error": "转派目标人员不存在或已禁用"}), 400

    if task['task_type'] == 'review' and to_user['role'] not in ('reviewer', 'admin'):
        return jsonify({"error": "阅卷任务只能转派给阅卷员或管理员"}), 400
    if task['task_type'] == 'audit' and to_user['role'] not in ('auditor', 'admin'):
        return jsonify({"error": "复核任务只能转派给复核员或管理员"}), 400

    if to_user_id == supervision['supervisee_id']:
        return jsonify({"error": "不能转派给当前被督办人"}), 400

    now = datetime.now()
    from_user_id = supervision['supervisee_id']

    db.execute("""
        INSERT INTO task_reassignments (
            supervision_id, task_id, paper_id, from_user_id, to_user_id, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [supervision_id, supervision['task_id'], supervision['paper_id'],
          from_user_id, to_user_id, reason, now])

    db.execute("""
        UPDATE tasks SET assignee_id = ? WHERE id = ?
    """, [to_user_id, supervision['task_id']])

    review_type = 'initial' if task['task_type'] == 'review' else 'audit'
    existing_review = db.execute("""
        SELECT id FROM reviews WHERE task_id = ? AND review_type = ?
    """, [supervision['task_id'], review_type]).fetchone()

    if existing_review:
        db.execute("""
            UPDATE reviews SET reviewer_id = ?, updated_at = ? WHERE id = ?
        """, [to_user_id, now, existing_review['id']])
    else:
        paper = db.execute("SELECT current_round FROM papers WHERE id = ?", [task['paper_id']]).fetchone()
        review_round = paper['current_round'] if paper else 1
        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type, review_round)
            VALUES (?, ?, ?, ?, ?)
        """, [supervision['task_id'], task['paper_id'], to_user_id, review_type, review_round])

    db.execute("""
        UPDATE task_supervisions SET supervisee_id = ?, updated_at = ? WHERE id = ?
    """, [to_user_id, now, supervision_id])

    from_user = db.execute("SELECT real_name, username FROM users WHERE id = ?", [from_user_id]).fetchone()
    add_supervision_log(db, supervision_id, user['id'], 'reassign',
                        f"从{from_user['real_name'] or from_user['username']}转派给{to_user['real_name'] or to_user['username']}" + (f"，原因：{reason}" if reason else ""),
                        supervision['status'], supervision['status'])

    paper = db.execute("SELECT paper_number FROM papers WHERE id = ?", [supervision['paper_id']]).fetchone()
    paper_number = paper['paper_number'] if paper else str(supervision['paper_id'])
    try:
        db.execute("""
            INSERT INTO alerts (alert_type, alert_level, paper_id, task_id, supervision_id, message, detail_json)
            VALUES ('task_supervision', 'warning', ?, ?, ?, ?, ?)
        """, [
            supervision['paper_id'], supervision['task_id'], supervision_id,
            f"任务转派：试卷{paper_number}，转派给{to_user['real_name'] or to_user['username']}",
            f'{{"supervision_id": {supervision_id}, "from_user_id": {from_user_id}, "to_user_id": {to_user_id}}}'
        ])
    except Exception:
        db.execute("""
            INSERT INTO alerts (alert_type, alert_level, paper_id, task_id, message, detail_json)
            VALUES ('task_supervision', 'warning', ?, ?, ?, ?)
        """, [
            supervision['paper_id'], supervision['task_id'],
            f"任务转派：试卷{paper_number}，转派给{to_user['real_name'] or to_user['username']}",
            f'{{"supervision_id": {supervision_id}, "from_user_id": {from_user_id}, "to_user_id": {to_user_id}}}'
        ])

    db.commit()

    return jsonify({
        "message": f"任务已转派给{to_user['real_name'] or to_user['username']}",
        "to_user_id": to_user_id
    }), 200


@supervision_bp.route('/my-pending', methods=['GET'])
@role_required('reviewer', 'auditor', 'admin')
def my_pending_supervisions():
    user = get_current_user()
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    base_query = """
        FROM task_supervisions ts
        LEFT JOIN tasks t ON ts.task_id = t.id
        LEFT JOIN papers p ON ts.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users su ON ts.supervisor_id = su.id
        WHERE ts.supervisee_id = ? AND ts.status IN ('pending', 'acknowledged', 'processing', 'overdue')
    """
    params = [user['id']]

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT ts.*, t.task_code, t.task_type, t.status as task_status,
               p.paper_number, p.candidate_name, p.current_status as paper_status,
               b.batch_name, qg.group_name as question_group_name,
               su.real_name as supervisor_name, su.username as supervisor_username
        {base_query}
        ORDER BY
            CASE ts.urgency_level
                WHEN 'critical' THEN 0
                WHEN 'urgent' THEN 1
                ELSE 2
            END,
            CASE ts.status
                WHEN 'pending' THEN 0
                WHEN 'overdue' THEN 1
                WHEN 'acknowledged' THEN 2
                ELSE 3
            END,
            ts.expected_complete_at ASC NULLS LAST
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = SUPERVISION_STATUS_MAP.get(d['status'], d['status'])
        d['urgency_level_name'] = SUPERVISION_URGENCY_MAP.get(d['urgency_level'], d['urgency_level'])
        d['task_status_name'] = STATUS_MAP.get(d['task_status'], d['task_status']) if d.get('task_status') else None
        d['paper_status_name'] = STATUS_MAP.get(d['paper_status'], d['paper_status']) if d.get('paper_status') else None

        if d.get('expected_complete_at'):
            exp = d['expected_complete_at']
            if isinstance(exp, str):
                exp = datetime.fromisoformat(exp)
            d['is_overdue'] = exp < datetime.now()
            d['hours_remaining'] = round((exp - datetime.now()).total_seconds() / 3600, 1)
        else:
            d['is_overdue'] = False

        result.append(d)

    priority_count = db.execute("""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN ts.urgency_level = 'critical' THEN 1 ELSE 0 END), 0) as critical_count,
            COALESCE(SUM(CASE WHEN ts.urgency_level = 'urgent' THEN 1 ELSE 0 END), 0) as urgent_count,
            COALESCE(SUM(CASE WHEN ts.status = 'pending' THEN 1 ELSE 0 END), 0) as pending_count,
            COALESCE(SUM(CASE WHEN ts.status = 'overdue' THEN 1 ELSE 0 END), 0) as overdue_count
        FROM task_supervisions ts
        WHERE ts.supervisee_id = ? AND ts.status IN ('pending', 'acknowledged', 'processing', 'overdue')
    """, [user['id']]).fetchone()

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "priority_summary": dict(priority_count),
        "items": result
    }), 200


@supervision_bp.route('/<int:supervision_id>/acknowledge', methods=['POST'])
@role_required('reviewer', 'auditor', 'admin')
def acknowledge_supervision(supervision_id):
    data = request.get_json(force=True, silent=True) or {}
    user = get_current_user()
    db = get_db()

    remark = (data.get('remark') or '').strip()

    supervision = db.execute("""
        SELECT id, supervisee_id, status FROM task_supervisions WHERE id = ?
    """, [supervision_id]).fetchone()

    if not supervision:
        return jsonify({"error": "督办记录不存在"}), 404

    if supervision['supervisee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此督办"}), 403

    if supervision['status'] != 'pending':
        return jsonify({"error": "只有待确认状态的督办可以确认"}), 400

    now = datetime.now()
    db.execute("""
        UPDATE task_supervisions SET status = 'acknowledged', updated_at = ? WHERE id = ?
    """, [now, supervision_id])

    add_supervision_log(db, supervision_id, user['id'], 'acknowledge', remark or '已确认督办', 'pending', 'acknowledged')
    db.commit()

    return jsonify({"message": "督办已确认"}), 200


@supervision_bp.route('/<int:supervision_id>/feedback', methods=['POST'])
@role_required('reviewer', 'auditor', 'admin')
def submit_feedback(supervision_id):
    data = request.get_json(force=True, silent=True) or {}
    user = get_current_user()
    db = get_db()

    feedback = (data.get('feedback') or '').strip()

    if not feedback:
        return jsonify({"error": "反馈说明为必填项"}), 400

    supervision = db.execute("""
        SELECT id, supervisee_id, status FROM task_supervisions WHERE id = ?
    """, [supervision_id]).fetchone()

    if not supervision:
        return jsonify({"error": "督办记录不存在"}), 404

    if supervision['supervisee_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({"error": "无权操作此督办"}), 403

    if supervision['status'] not in ('pending', 'acknowledged', 'processing', 'overdue'):
        return jsonify({"error": f"当前状态{supervision['status']}不允许提交反馈"}), 400

    now = datetime.now()

    task = db.execute("""
        SELECT id, status FROM tasks WHERE id = (SELECT task_id FROM task_supervisions WHERE id = ?)
    """, [supervision_id]).fetchone()

    task_completed = False
    if task and task['status'] in ('finalized', 'pending_audit', 'diff_pending'):
        task_completed = True

    new_status = 'completed' if task_completed else 'processing'

    db.execute("""
        UPDATE task_supervisions
        SET feedback = ?, feedback_at = ?, status = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
    """, [feedback, now, new_status, now if new_status == 'completed' else None, now, supervision_id])

    add_supervision_log(db, supervision_id, user['id'], 'feedback', f"提交反馈：{feedback[:100]}", supervision['status'], new_status)

    if new_status == 'completed':
        try:
            db.execute("""
                UPDATE alerts SET is_handled = true, handled_at = ?
                WHERE supervision_id = ? AND is_handled = false
            """, [now, supervision_id])
        except Exception:
            pass

    db.commit()

    return jsonify({
        "message": "反馈已提交",
        "new_status": new_status,
        "new_status_name": SUPERVISION_STATUS_MAP.get(new_status, new_status)
    }), 200


@supervision_bp.route('/paper/<int:paper_id>', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def paper_supervisions(paper_id):
    db = get_db()

    result = get_paper_supervision_info(db, paper_id)

    for item in result:
        reassignments = db.execute("""
            SELECT tr.*, u1.real_name as from_user_name, u2.real_name as to_user_name
            FROM task_reassignments tr
            LEFT JOIN users u1 ON tr.from_user_id = u1.id
            LEFT JOIN users u2 ON tr.to_user_id = u2.id
            WHERE tr.supervision_id = ?
            ORDER BY tr.id ASC
        """, [item['id']]).fetchall()
        item['reassignments'] = [dict(ra) for ra in reassignments]

    return jsonify(result), 200


@supervision_bp.route('/statistics', methods=['GET'])
@role_required('admin')
def supervision_statistics():
    db = get_db()

    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    base_where = "WHERE 1=1"
    params = []

    if batch_id:
        base_where += " AND p.batch_id = ?"
        params.append(batch_id)
    if question_group_id:
        base_where += " AND p.question_group_id = ?"
        params.append(question_group_id)
    if date_from:
        base_where += " AND ts.created_at >= ?"
        params.append(date_from)
    if date_to:
        base_where += " AND ts.created_at <= ?"
        params.append(date_to + " 23:59:59")

    try:
        by_status = db.execute(f"""
            SELECT ts.status, COUNT(*) as count
            FROM task_supervisions ts
            LEFT JOIN papers p ON ts.paper_id = p.id
            {base_where}
            GROUP BY ts.status
            ORDER BY ts.status
        """, list(params)).fetchall()
    except Exception:
        by_status = []

    status_list = []
    for s in by_status:
        d = dict(s)
        d['status_name'] = SUPERVISION_STATUS_MAP.get(d['status'], d['status'])
        status_list.append(d)

    try:
        by_urgency = db.execute(f"""
            SELECT ts.urgency_level, COUNT(*) as count
            FROM task_supervisions ts
            LEFT JOIN papers p ON ts.paper_id = p.id
            {base_where}
            GROUP BY ts.urgency_level
            ORDER BY ts.urgency_level
        """, list(params)).fetchall()
    except Exception:
        by_urgency = []

    urgency_list = []
    for u in by_urgency:
        d = dict(u)
        d['urgency_level_name'] = SUPERVISION_URGENCY_MAP.get(d['urgency_level'], d['urgency_level'])
        urgency_list.append(d)

    try:
        by_supervisee = db.execute(f"""
            SELECT ts.supervisee_id, u.real_name as supervisee_name, u.username as supervisee_username,
                   COUNT(*) as supervision_count,
                   SUM(CASE WHEN ts.status = 'completed' THEN 1 ELSE 0 END) as completed_count,
                   SUM(CASE WHEN ts.status IN ('pending', 'acknowledged', 'processing', 'overdue') THEN 1 ELSE 0 END) as pending_count
            FROM task_supervisions ts
            LEFT JOIN users u ON ts.supervisee_id = u.id
            LEFT JOIN papers p ON ts.paper_id = p.id
            {base_where}
            GROUP BY ts.supervisee_id, u.real_name, u.username
            ORDER BY supervision_count DESC
        """, list(params)).fetchall()
    except Exception:
        by_supervisee = []

    try:
        by_batch = db.execute(f"""
            SELECT p.batch_id, b.batch_name, COUNT(*) as count
            FROM task_supervisions ts
            LEFT JOIN papers p ON ts.paper_id = p.id
            LEFT JOIN batches b ON p.batch_id = b.id
            {base_where}
            GROUP BY p.batch_id, b.batch_name
            ORDER BY count DESC
        """, list(params)).fetchall()
    except Exception:
        by_batch = []

    try:
        by_question_group = db.execute(f"""
            SELECT p.question_group_id, qg.group_name, COUNT(*) as count
            FROM task_supervisions ts
            LEFT JOIN papers p ON ts.paper_id = p.id
            LEFT JOIN question_groups qg ON p.question_group_id = qg.id
            {base_where}
            GROUP BY p.question_group_id, qg.group_name
            ORDER BY count DESC
        """, list(params)).fetchall()
    except Exception:
        by_question_group = []

    try:
        reassignment_stats = db.execute(f"""
            SELECT COUNT(*) as total_reassignments,
                   COUNT(DISTINCT tr.supervision_id) as tasks_with_reassignment
            FROM task_reassignments tr
            LEFT JOIN task_supervisions ts ON tr.supervision_id = ts.id
            LEFT JOIN papers p ON ts.paper_id = p.id
            {base_where}
        """, list(params)).fetchone()
    except Exception:
        reassignment_stats = None

    try:
        total = db.execute(f"""
            SELECT COUNT(*) FROM task_supervisions ts
            LEFT JOIN papers p ON ts.paper_id = p.id
            {base_where}
        """, list(params)).fetchval()
    except Exception:
        total = 0

    try:
        overdue_count = db.execute(f"""
            SELECT COUNT(*) FROM task_supervisions ts
            LEFT JOIN papers p ON ts.paper_id = p.id
            {base_where}
              AND ts.status IN ('pending', 'acknowledged', 'processing')
              AND ts.expected_complete_at IS NOT NULL
              AND ts.expected_complete_at < CURRENT_TIMESTAMP
        """, list(params)).fetchval()
    except Exception:
        overdue_count = 0

    return jsonify({
        "total": total or 0,
        "overdue_count": overdue_count or 0,
        "by_status": status_list,
        "by_urgency": urgency_list,
        "by_supervisee": rows_to_list(by_supervisee),
        "by_batch": rows_to_list(by_batch),
        "by_question_group": rows_to_list(by_question_group),
        "reassignment_stats": dict(reassignment_stats) if reassignment_stats else {}
    }), 200


@supervision_bp.route('/check-overdue', methods=['POST'])
@role_required('admin')
def check_overdue():
    db = get_db()
    now = datetime.now()

    overdue_rows = db.execute("""
        SELECT id, supervision_code, paper_id, task_id, supervisee_id
        FROM task_supervisions
        WHERE status IN ('pending', 'acknowledged', 'processing')
          AND expected_complete_at IS NOT NULL
          AND expected_complete_at < ?
    """, [now]).fetchall()

    updated = 0
    for row in overdue_rows:
        db.execute("""
            UPDATE task_supervisions SET status = 'overdue', updated_at = ? WHERE id = ?
        """, [now, row['id']])
        add_supervision_log(db, row['id'], None, 'overdue', '督办已逾期', None, 'overdue')

        paper = db.execute("SELECT paper_number FROM papers WHERE id = ?", [row['paper_id']]).fetchone()
        paper_number = paper['paper_number'] if paper else str(row['paper_id'])
        try:
            db.execute("""
                INSERT INTO alerts (alert_type, alert_level, paper_id, task_id, supervision_id, message, detail_json)
                VALUES ('task_supervision', 'critical', ?, ?, ?, ?, ?)
            """, [
                row['paper_id'], row['task_id'], row['id'],
                f"督办逾期：试卷{paper_number}，督办单{row['supervision_code']}已超过期望完成时间",
                f'{{"supervision_id": {row["id"]}, "supervision_code": "{row["supervision_code"]}"}}'
            ])
        except Exception:
            db.execute("""
                INSERT INTO alerts (alert_type, alert_level, paper_id, task_id, message, detail_json)
                VALUES ('task_supervision', 'critical', ?, ?, ?, ?)
            """, [
                row['paper_id'], row['task_id'],
                f"督办逾期：试卷{paper_number}，督办单{row['supervision_code']}已超过期望完成时间",
                f'{{"supervision_id": {row["id"]}, "supervision_code": "{row["supervision_code"]}"}}'
            ])
        updated += 1

    db.commit()

    return jsonify({
        "message": "逾期检查完成",
        "overdue_count": updated
    }), 200
