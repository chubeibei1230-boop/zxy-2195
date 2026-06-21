from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

from app.db import get_db
from app.utils import (
    role_required, get_current_user, generate_return_code,
    get_paper_return_count, get_paper_current_round,
    ensure_single_active_task, update_paper_status, calculate_deadline,
    generate_task_code, detect_anomalies,
    STATUS_MAP, ROLE_MAP, RETURN_REASON_TYPE_MAP, RETURN_STATUS_MAP,
    SUPERVISION_STATUS_MAP, SUPERVISION_URGENCY_MAP
)
from config import Config

return_bp = Blueprint('returns', __name__, url_prefix='/api/returns')


@return_bp.route('', methods=['POST'])
@role_required('auditor', 'admin')
def create_return():
    data = request.get_json(force=True, silent=True) or {}
    user = get_current_user()
    db = get_db()

    paper_id = data.get('paper_id')
    task_id = data.get('task_id')
    return_reason = (data.get('return_reason') or '').strip()
    return_reason_type = data.get('return_reason_type')
    handling_opinion = (data.get('handling_opinion') or '').strip()
    assignee_id = data.get('assignee_id')

    if not paper_id or not return_reason or not return_reason_type:
        return jsonify({"error": "试卷ID、退回原因、退回原因类型为必填项"}), 400

    if return_reason_type not in RETURN_REASON_TYPE_MAP:
        return jsonify({"error": "无效的退回原因类型"}), 400

    paper = db.execute("""
        SELECT id, paper_number, current_status, current_round, return_count,
               question_group_id, batch_id
        FROM papers WHERE id = ?
    """, [paper_id]).fetchone()

    if not paper:
        return jsonify({"error": "试卷不存在"}), 404

    if paper['current_status'] not in ('pending_audit', 'diff_pending'):
        return jsonify({"error": "试卷当前状态不允许发起退回重评，仅待复核、差异待处理状态可退回"}), 400

    if task_id:
        task = db.execute("""
            SELECT id, paper_id, task_type, status, is_active
            FROM tasks WHERE id = ?
        """, [task_id]).fetchone()
        if not task or task['paper_id'] != paper_id:
            return jsonify({"error": "任务与试卷不匹配"}), 400
        if task['task_type'] != 'audit':
            return jsonify({"error": "只能退回复核任务"}), 400

    now = datetime.now()
    return_code = generate_return_code()
    return_round = (paper['return_count'] or 0) + 1
    new_review_round = (paper['current_round'] or 1) + 1

    cursor = db.execute("""
        INSERT INTO review_return_records (
            return_code, paper_id, task_id, auditor_id, return_reason,
            return_reason_type, handling_opinion, return_round, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        RETURNING id
    """, [return_code, paper_id, task_id, user['id'], return_reason,
          return_reason_type, handling_opinion, return_round, now])
    return_id = cursor.fetchval()

    if task_id:
        db.execute("""
            UPDATE tasks SET status = 'returned', is_active = false WHERE id = ?
        """, [task_id])

    db.execute("""
        UPDATE papers SET
            current_status = 'pending_reeval',
            current_round = ?,
            return_count = ?,
            latest_return_reason = ?,
            latest_return_reason_type = ?
        WHERE id = ?
    """, [new_review_round, return_round, return_reason, return_reason_type, paper_id])

    paper_info = db.execute("""
        SELECT p.question_group_id FROM papers p WHERE p.id = ?
    """, [paper_id]).fetchone()

    rg = None
    if paper_info:
        rg = db.execute("""
            SELECT id, review_time_limit_hours
            FROM responsibility_groups
            WHERE question_group_id = ? AND (batch_id = ? OR batch_id IS NULL)
            ORDER BY batch_id DESC NULLS LAST LIMIT 1
        """, [paper_info['question_group_id'], paper['batch_id']]).fetchone()

    final_group_id = rg['id'] if rg else None
    deadline_hours = float(rg['review_time_limit_hours']) if rg else Config.REVIEW_TIMEOUT_HOURS
    deadline = calculate_deadline(deadline_hours)

    active_task = ensure_single_active_task(db, paper_id, 'review')
    if active_task:
        db.execute("UPDATE tasks SET is_active = false WHERE id = ?", [active_task['id']])

    task_code = generate_task_code('review')

    if not assignee_id:
        last_reviewer = db.execute("""
            SELECT t.assignee_id FROM tasks t
            WHERE t.paper_id = ? AND t.task_type = 'review'
            ORDER BY t.id DESC LIMIT 1
        """, [paper_id]).fetchone()
        if last_reviewer and last_reviewer['assignee_id']:
            same_user = db.execute(
                "SELECT id FROM users WHERE id = ? AND is_active = true AND role IN ('reviewer', 'admin')",
                [last_reviewer['assignee_id']]
            ).fetchone()
            if same_user:
                assignee_id = last_reviewer['assignee_id']

    if assignee_id:
        assignee = db.execute(
            "SELECT id, role FROM users WHERE id = ? AND is_active = true",
            [assignee_id]
        ).fetchone()
        if not assignee or assignee['role'] not in ('reviewer', 'admin'):
            assignee_id = None

    task_cursor = db.execute("""
        INSERT INTO tasks (
            task_code, paper_id, task_type, assignee_id, group_id, status,
            assigned_at, deadline_at, is_active, review_round, return_record_id
        ) VALUES (?, ?, 'review', ?, ?, 'pending_reeval', ?, ?, true, ?, ?)
        RETURNING id
    """, [task_code, paper_id, assignee_id, final_group_id,
          now, deadline, new_review_round, return_id])
    new_task_id = task_cursor.fetchval()

    if assignee_id:
        db.execute("""
            INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type, review_round)
            VALUES (?, ?, ?, 'initial', ?)
        """, [new_task_id, paper_id, assignee_id, new_review_round])

    db.execute("""
        UPDATE review_return_records SET reeval_task_id = ?, status = 'reevaluating', updated_at = ?
        WHERE id = ?
    """, [new_task_id, now, return_id])

    alert_level = 'critical' if return_round >= 3 else ('warning' if return_round >= 2 else 'info')
    reason_name = RETURN_REASON_TYPE_MAP.get(return_reason_type, return_reason_type)
    db.execute("""
        INSERT INTO alerts (alert_type, alert_level, paper_id, message, detail_json)
        VALUES ('return_reeval', ?, ?, ?, ?)
    """, [
        alert_level,
        paper_id,
        f"退回重评：试卷{paper['paper_number']}，第{return_round}轮退回，原因：{reason_name}",
        f'{{"return_id": {return_id}, "return_code": "{return_code}", "return_round": {return_round}, "return_reason_type": "{return_reason_type}"}}'
    ])

    db.commit()

    return jsonify({
        "id": return_id,
        "return_code": return_code,
        "return_round": return_round,
        "new_task_id": new_task_id,
        "task_code": task_code,
        "message": "退回重评已发起，试卷已进入待重评状态"
    }), 201


@return_bp.route('', methods=['GET'])
@role_required('admin', 'auditor')
def list_returns():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    status = request.args.get('status')
    return_reason_type = request.args.get('return_reason_type')
    paper_id = request.args.get('paper_id', type=int)
    auditor_id = request.args.get('auditor_id', type=int)
    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    return_round_min = request.args.get('return_round_min', type=int)

    base_query = """
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users u ON rr.auditor_id = u.id
        LEFT JOIN tasks t ON rr.reeval_task_id = t.id
        WHERE 1=1
    """
    params = []

    if status:
        base_query += " AND rr.status = ?"
        params.append(status)
    if return_reason_type:
        base_query += " AND rr.return_reason_type = ?"
        params.append(return_reason_type)
    if paper_id:
        base_query += " AND rr.paper_id = ?"
        params.append(paper_id)
    if auditor_id:
        base_query += " AND rr.auditor_id = ?"
        params.append(auditor_id)
    if batch_id:
        base_query += " AND p.batch_id = ?"
        params.append(batch_id)
    if question_group_id:
        base_query += " AND p.question_group_id = ?"
        params.append(question_group_id)
    if return_round_min:
        base_query += " AND rr.return_round >= ?"
        params.append(return_round_min)

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT rr.*, p.paper_number, p.candidate_name, p.current_status as paper_status,
               p.current_round, p.return_count,
               b.batch_name, b.batch_code, qg.group_name as question_group_name,
               u.real_name as auditor_name, u.username as auditor_username,
               t.task_code as reeval_task_code, t.status as reeval_task_status
        {base_query}
        ORDER BY rr.id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = RETURN_STATUS_MAP.get(d['status'], d['status'])
        d['return_reason_type_name'] = RETURN_REASON_TYPE_MAP.get(d['return_reason_type'], d['return_reason_type'])
        d['paper_status_name'] = STATUS_MAP.get(d['paper_status'], d['paper_status'])
        if d.get('reeval_task_status'):
            d['reeval_task_status_name'] = STATUS_MAP.get(d['reeval_task_status'], d['reeval_task_status'])
        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@return_bp.route('/<int:return_id>', methods=['GET'])
@role_required('admin', 'auditor', 'reviewer')
def get_return_detail(return_id):
    db = get_db()

    record = db.execute("""
        SELECT rr.*, p.paper_number, p.candidate_name, p.candidate_id,
               p.current_status as paper_status, p.current_round, p.return_count,
               p.paper_content, p.storage_path,
               b.batch_name, b.batch_code, qg.group_name as question_group_name,
               qg.max_score, qg.pass_score,
               u.real_name as auditor_name, u.username as auditor_username,
               t.task_code as reeval_task_code, t.status as reeval_task_status,
               t.assignee_id as reeval_assignee_id
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users u ON rr.auditor_id = u.id
        LEFT JOIN tasks t ON rr.reeval_task_id = t.id
        WHERE rr.id = ?
    """, [return_id]).fetchone()

    if not record:
        return jsonify({"error": "退回记录不存在"}), 404

    result = dict(record)
    result['status_name'] = RETURN_STATUS_MAP.get(result['status'], result['status'])
    result['return_reason_type_name'] = RETURN_REASON_TYPE_MAP.get(result['return_reason_type'], result['return_reason_type'])
    result['paper_status_name'] = STATUS_MAP.get(result['paper_status'], result['paper_status'])
    if result.get('reeval_task_status'):
        result['reeval_task_status_name'] = STATUS_MAP.get(result['reeval_task_status'], result['reeval_task_status'])

    history = db.execute("""
        SELECT rr.*, u.real_name as auditor_name
        FROM review_return_records rr
        LEFT JOIN users u ON rr.auditor_id = u.id
        WHERE rr.paper_id = ?
        ORDER BY rr.id ASC
    """, [record['paper_id']]).fetchall()

    result['return_history'] = [{
        'id': h['id'],
        'return_code': h['return_code'],
        'return_reason': h['return_reason'],
        'return_reason_type': h['return_reason_type'],
        'return_reason_type_name': RETURN_REASON_TYPE_MAP.get(h['return_reason_type'], h['return_reason_type']),
        'handling_opinion': h['handling_opinion'],
        'return_round': h['return_round'],
        'status': h['status'],
        'status_name': RETURN_STATUS_MAP.get(h['status'], h['status']),
        'auditor_name': h['auditor_name'],
        'created_at': str(h['created_at']) if h['created_at'] else None,
        'reevaluated_at': str(h['reevaluated_at']) if h['reevaluated_at'] else None,
        'closed_at': str(h['closed_at']) if h['closed_at'] else None,
    } for h in history]

    return jsonify(result), 200


@return_bp.route('/paper/<int:paper_id>/timeline', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def paper_timeline(paper_id):
    db = get_db()

    paper = db.execute("""
        SELECT p.*, b.batch_name, qg.group_name as question_group_name,
               qg.max_score, qg.pass_score
        FROM papers p
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        WHERE p.id = ?
    """, [paper_id]).fetchone()

    if not paper:
        return jsonify({"error": "试卷不存在"}), 404

    timeline = []

    reviews = db.execute("""
        SELECT r.id, r.task_id, r.reviewer_id, r.review_type, r.initial_score,
               r.audit_score, r.final_score, r.deduction_reason, r.difficulty_flag,
               r.difficulty_note, r.completion_note, r.diff_reason, r.handling_opinion,
               r.review_round, r.created_at, r.updated_at,
               u.real_name as reviewer_name, u.username as reviewer_username,
               t.task_code, t.status as task_status, t.assigned_at, t.completed_at
        FROM reviews r
        LEFT JOIN users u ON r.reviewer_id = u.id
        LEFT JOIN tasks t ON r.task_id = t.id
        WHERE r.paper_id = ?
        ORDER BY r.created_at ASC
    """, [paper_id]).fetchall()

    for r in reviews:
        event_type = 'initial_review' if r['review_type'] == 'initial' else 'audit_review'
        timeline.append({
            'event_type': event_type,
            'event_type_name': '初评' if r['review_type'] == 'initial' else '复核',
            'round': r['review_round'] or 1,
            'reviewer_name': r['reviewer_name'],
            'reviewer_username': r['reviewer_username'],
            'task_code': r['task_code'],
            'task_status': r['task_status'],
            'task_status_name': STATUS_MAP.get(r['task_status'], r['task_status']) if r['task_status'] else None,
            'initial_score': float(r['initial_score']) if r['initial_score'] is not None else None,
            'audit_score': float(r['audit_score']) if r['audit_score'] is not None else None,
            'final_score': float(r['final_score']) if r['final_score'] is not None else None,
            'deduction_reason': r['deduction_reason'],
            'difficulty_flag': r['difficulty_flag'],
            'difficulty_note': r['difficulty_note'],
            'completion_note': r['completion_note'],
            'diff_reason': r['diff_reason'],
            'handling_opinion': r['handling_opinion'],
            'created_at': str(r['created_at']) if r['created_at'] else None,
            'updated_at': str(r['updated_at']) if r['updated_at'] else None,
            'assigned_at': str(r['assigned_at']) if r['assigned_at'] else None,
            'completed_at': str(r['completed_at']) if r['completed_at'] else None,
        })

    returns = db.execute("""
        SELECT rr.*, u.real_name as auditor_name, u.username as auditor_username,
               t.task_code as reeval_task_code
        FROM review_return_records rr
        LEFT JOIN users u ON rr.auditor_id = u.id
        LEFT JOIN tasks t ON rr.reeval_task_id = t.id
        WHERE rr.paper_id = ?
        ORDER BY rr.created_at ASC
    """, [paper_id]).fetchall()

    for rr in returns:
        timeline.append({
            'event_type': 'return_reeval',
            'event_type_name': '退回重评',
            'round': rr['return_round'],
            'return_code': rr['return_code'],
            'return_reason': rr['return_reason'],
            'return_reason_type': rr['return_reason_type'],
            'return_reason_type_name': RETURN_REASON_TYPE_MAP.get(rr['return_reason_type'], rr['return_reason_type']),
            'handling_opinion': rr['handling_opinion'],
            'auditor_name': rr['auditor_name'],
            'auditor_username': rr['auditor_username'],
            'status': rr['status'],
            'status_name': RETURN_STATUS_MAP.get(rr['status'], rr['status']),
            'reeval_task_code': rr['reeval_task_code'],
            'created_at': str(rr['created_at']) if rr['created_at'] else None,
            'reevaluated_at': str(rr['reevaluated_at']) if rr['reevaluated_at'] else None,
            'closed_at': str(rr['closed_at']) if rr['closed_at'] else None,
        })

    supervisions = db.execute("""
        SELECT ts.*, u.real_name as supervisor_name, u.username as supervisor_username,
               s.real_name as supervisee_name, s.username as supervisee_username,
               t.task_code, t.task_type
        FROM task_supervisions ts
        LEFT JOIN users u ON ts.supervisor_id = u.id
        LEFT JOIN users s ON ts.supervisee_id = s.id
        LEFT JOIN tasks t ON ts.task_id = t.id
        WHERE ts.paper_id = ?
        ORDER BY ts.created_at ASC
    """, [paper_id]).fetchall()

    for sv in supervisions:
        reassignments = db.execute("""
            SELECT tr.*, u1.real_name as from_user_name, u2.real_name as to_user_name
            FROM task_reassignments tr
            LEFT JOIN users u1 ON tr.from_user_id = u1.id
            LEFT JOIN users u2 ON tr.to_user_id = u2.id
            WHERE tr.supervision_id = ?
            ORDER BY tr.id ASC
        """, [sv['id']]).fetchall()

        event = {
            'event_type': 'supervision',
            'event_type_name': '督办',
            'supervision_code': sv['supervision_code'],
            'reason': sv['reason'],
            'urgency_level': sv['urgency_level'],
            'urgency_level_name': SUPERVISION_URGENCY_MAP.get(sv['urgency_level'], sv['urgency_level']),
            'requirements': sv['requirements'],
            'supervisor_name': sv['supervisor_name'],
            'supervisee_name': sv['supervisee_name'],
            'status': sv['status'],
            'status_name': SUPERVISION_STATUS_MAP.get(sv['status'], sv['status']),
            'task_code': sv['task_code'],
            'task_type': sv['task_type'],
            'feedback': sv['feedback'],
            'created_at': str(sv['created_at']) if sv['created_at'] else None,
            'expected_complete_at': str(sv['expected_complete_at']) if sv['expected_complete_at'] else None,
            'completed_at': str(sv['completed_at']) if sv['completed_at'] else None,
            'reassignments': [{
                'from_user_name': ra['from_user_name'],
                'to_user_name': ra['to_user_name'],
                'reason': ra['reason'],
                'created_at': str(ra['created_at']) if ra['created_at'] else None,
            } for ra in reassignments],
        }
        timeline.append(event)

    timeline.sort(key=lambda x: x.get('created_at') or '', reverse=False)

    latest_return = None
    if returns:
        latest = returns[-1]
        latest_return = {
            'return_code': latest['return_code'],
            'return_reason': latest['return_reason'],
            'return_reason_type': latest['return_reason_type'],
            'return_reason_type_name': RETURN_REASON_TYPE_MAP.get(latest['return_reason_type'], latest['return_reason_type']),
            'handling_opinion': latest['handling_opinion'],
            'return_round': latest['return_round'],
            'created_at': str(latest['created_at']) if latest['created_at'] else None,
        }

    paper_info = dict(paper)
    paper_info['status_name'] = STATUS_MAP.get(paper_info['current_status'], paper_info['current_status'])

    latest_review = db.execute("""
        SELECT r.final_score FROM reviews r
        WHERE r.paper_id = ? AND r.final_score IS NOT NULL
        ORDER BY r.updated_at DESC LIMIT 1
    """, [paper_id]).fetchone()

    return jsonify({
        "paper": paper_info,
        "current_round": paper['current_round'],
        "return_count": paper['return_count'],
        "latest_return": latest_return,
        "final_score": float(latest_review['final_score']) if latest_review and latest_review['final_score'] is not None else None,
        "timeline": timeline,
        "total_events": len(timeline)
    }), 200


@return_bp.route('/statistics', methods=['GET'])
@role_required('admin')
def return_statistics():
    db = get_db()

    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    auditor_id = request.args.get('auditor_id', type=int)
    return_reason_type = request.args.get('return_reason_type')
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
    if auditor_id:
        base_where += " AND rr.auditor_id = ?"
        params.append(auditor_id)
    if return_reason_type:
        base_where += " AND rr.return_reason_type = ?"
        params.append(return_reason_type)
    if date_from:
        base_where += " AND rr.created_at >= ?"
        params.append(date_from)
    if date_to:
        base_where += " AND rr.created_at <= ?"
        params.append(date_to + " 23:59:59")

    by_reason_type = db.execute(f"""
        SELECT rr.return_reason_type, COUNT(*) as count
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        {base_where}
        GROUP BY rr.return_reason_type
        ORDER BY count DESC
    """, params).fetchall()

    reason_list = []
    for r in by_reason_type:
        d = dict(r)
        d['return_reason_type_name'] = RETURN_REASON_TYPE_MAP.get(d['return_reason_type'], d['return_reason_type'])
        reason_list.append(d)

    by_batch = db.execute(f"""
        SELECT p.batch_id, b.batch_name, COUNT(*) as count
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        {base_where}
        GROUP BY p.batch_id, b.batch_name
        ORDER BY count DESC
    """, list(params)).fetchall()

    by_question_group = db.execute(f"""
        SELECT p.question_group_id, qg.group_name, COUNT(*) as count
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        {base_where}
        GROUP BY p.question_group_id, qg.group_name
        ORDER BY count DESC
    """, list(params)).fetchall()

    by_auditor = db.execute(f"""
        SELECT rr.auditor_id, u.real_name as auditor_name, COUNT(*) as count
        FROM review_return_records rr
        LEFT JOIN users u ON rr.auditor_id = u.id
        LEFT JOIN papers p ON rr.paper_id = p.id
        {base_where}
        GROUP BY rr.auditor_id, u.real_name
        ORDER BY count DESC
    """, list(params)).fetchall()

    by_round = db.execute(f"""
        SELECT rr.return_round, COUNT(*) as count
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        {base_where}
        GROUP BY rr.return_round
        ORDER BY rr.return_round
    """, list(params)).fetchall()

    by_status = db.execute(f"""
        SELECT rr.status, COUNT(*) as count
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        {base_where}
        GROUP BY rr.status
        ORDER BY rr.status
    """, list(params)).fetchall()

    status_list = []
    for s in by_status:
        d = dict(s)
        d['status_name'] = RETURN_STATUS_MAP.get(d['status'], d['status'])
        status_list.append(d)

    repeated_returns = db.execute(f"""
        SELECT rr.paper_id, p.paper_number, COUNT(*) as return_count,
               MAX(rr.return_round) as max_round,
               MIN(rr.created_at) as first_return_at,
               MAX(rr.created_at) as latest_return_at
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        {base_where}
        GROUP BY rr.paper_id, p.paper_number
        HAVING COUNT(*) >= 2
        ORDER BY return_count DESC
    """, list(params)).fetchall()

    long_unclosed = db.execute(f"""
        SELECT rr.*, p.paper_number, p.current_status as paper_status,
               u.real_name as auditor_name,
               t.task_code as reeval_task_code, t.status as reeval_task_status
        FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        LEFT JOIN users u ON rr.auditor_id = u.id
        LEFT JOIN tasks t ON rr.reeval_task_id = t.id
        {base_where}
          AND rr.status IN ('pending', 'reevaluating')
          AND rr.created_at < ?
        ORDER BY rr.created_at ASC
    """, list(params) + [datetime.now() - timedelta(hours=Config.REVIEW_TIMEOUT_HOURS)]).fetchall()

    unclosed_list = []
    for u in long_unclosed:
        d = dict(u)
        d['status_name'] = RETURN_STATUS_MAP.get(d['status'], d['status'])
        d['return_reason_type_name'] = RETURN_REASON_TYPE_MAP.get(d['return_reason_type'], d['return_reason_type'])
        d['paper_status_name'] = STATUS_MAP.get(d['paper_status'], d['paper_status'])
        if d.get('created_at'):
            created = d['created_at']
            if isinstance(created, str):
                created = datetime.fromisoformat(created)
            d['hours_pending'] = round((datetime.now() - created).total_seconds() / 3600, 1)
        unclosed_list.append(d)

    total = db.execute(f"""
        SELECT COUNT(*) FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        {base_where}
    """, list(params)).fetchval()

    pending_count = db.execute(f"""
        SELECT COUNT(*) FROM review_return_records rr
        LEFT JOIN papers p ON rr.paper_id = p.id
        {base_where} AND rr.status IN ('pending', 'reevaluating')
    """, list(params)).fetchval()

    return jsonify({
        "total": total,
        "pending_count": pending_count,
        "by_reason_type": reason_list,
        "by_batch": rows_to_list(by_batch),
        "by_question_group": rows_to_list(by_question_group),
        "by_auditor": by_auditor,
        "by_round": rows_to_list(by_round),
        "by_status": status_list,
        "repeated_returns": rows_to_list(repeated_returns),
        "long_unclosed": unclosed_list
    }), 200


def rows_to_list(rows):
    return [dict(r) for r in rows]
