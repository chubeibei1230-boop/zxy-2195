from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

from app.db import get_db
from app.utils import (
    role_required, rows_to_list, STATUS_MAP, ROLE_MAP,
    APPEAL_STATUS_MAP, APPEAL_TYPE_MAP, APPEAL_PRIORITY_MAP,
    get_paper_latest_appeal, RETURN_REASON_TYPE_MAP, RETURN_STATUS_MAP
)

query_bp = Blueprint('query', __name__, url_prefix='/api/query')


@query_bp.route('/papers', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def query_papers():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    status = request.args.get('status')
    reviewer_id = request.args.get('reviewer_id', type=int)
    auditor_id = request.args.get('auditor_id', type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    score_diff_min = request.args.get('score_diff_min', type=float)
    score_diff_max = request.args.get('score_diff_max', type=float)
    keyword = request.args.get('keyword', '').strip()

    base_query = """
        FROM papers p
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN review_appeals ra ON p.current_appeal_id = ra.id
        WHERE 1=1
    """
    params = []

    if batch_id:
        base_query += " AND p.batch_id = ?"
        params.append(batch_id)
    if question_group_id:
        base_query += " AND p.question_group_id = ?"
        params.append(question_group_id)
    if status:
        base_query += " AND p.current_status = ?"
        params.append(status)
    if keyword:
        base_query += " AND (p.paper_number LIKE ? OR p.candidate_name LIKE ? OR p.candidate_id LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    if date_from:
        base_query += " AND p.created_at >= ?"
        params.append(date_from)
    if date_to:
        base_query += " AND p.created_at <= ?"
        params.append(date_to + " 23:59:59")

    if reviewer_id or auditor_id or (score_diff_min is not None) or (score_diff_max is not None):
        base_query += """
            AND EXISTS (
                SELECT 1 FROM reviews r
                LEFT JOIN tasks t ON r.task_id = t.id
                WHERE r.paper_id = p.id
        """
        if reviewer_id:
            base_query += " AND r.review_type = 'initial' AND r.reviewer_id = ?"
            params.append(reviewer_id)
        if auditor_id:
            base_query += " AND r.review_type = 'audit' AND r.reviewer_id = ?"
            params.append(auditor_id)
        if score_diff_min is not None or score_diff_max is not None:
            base_query += """
                AND EXISTS (
                    SELECT 1 FROM reviews ri
                    JOIN reviews ra ON ri.paper_id = ra.paper_id
                    WHERE ri.paper_id = p.id
                      AND ri.review_type = 'initial' AND ra.review_type = 'audit'
                      AND ri.initial_score IS NOT NULL AND ra.audit_score IS NOT NULL
            """
            if score_diff_min is not None:
                base_query += " AND ABS(ri.initial_score - ra.audit_score) >= ?"
                params.append(score_diff_min)
            if score_diff_max is not None:
                base_query += " AND ABS(ri.initial_score - ra.audit_score) <= ?"
                params.append(score_diff_max)
            base_query += ")"
        base_query += ")"

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT p.*, b.batch_name, b.batch_code, qg.group_name as question_group_name,
               qg.group_code as question_group_code, qg.max_score, qg.pass_score,
               ra.id as current_appeal_id, ra.status as appeal_status,
               ra.appeal_type, ra.priority, ra.reason
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

        if d.get('appeal_status'):
            d['appeal_status_name'] = APPEAL_STATUS_MAP.get(d['appeal_status'], d['appeal_status'])
        if d.get('appeal_type'):
            d['appeal_type_name'] = APPEAL_TYPE_MAP.get(d['appeal_type'], d['appeal_type'])
        if d.get('appeal_count') and d['appeal_count'] > 0:
            from app.utils import APPEAL_PRIORITY_MAP
            history = db.execute("""
                SELECT id, appeal_code, appeal_type, status, priority, conclusion, final_score,
                       created_at, completed_at
                FROM review_appeals WHERE paper_id = ? ORDER BY id DESC
            """, [d['id']]).fetchall()
            d['appeal_history'] = [{
                'id': h['id'], 'appeal_code': h['appeal_code'],
                'appeal_type': h['appeal_type'],
                'appeal_type_name': APPEAL_TYPE_MAP.get(h['appeal_type'], h['appeal_type']),
                'status': h['status'],
                'status_name': APPEAL_STATUS_MAP.get(h['status'], h['status']),
                'priority': h['priority'],
                'priority_name': APPEAL_PRIORITY_MAP.get(h['priority'], h['priority']),
                'conclusion': h['conclusion'],
                'final_score': float(h['final_score']) if h['final_score'] is not None else None,
                'created_at': str(h['created_at']) if h['created_at'] else None,
                'completed_at': str(h['completed_at']) if h['completed_at'] else None,
            } for h in history]

        if d.get('return_count') and d['return_count'] > 0:
            return_history = db.execute("""
                SELECT id, return_code, return_reason, return_reason_type,
                       handling_opinion, return_round, status,
                       created_at, reevaluated_at, closed_at
                FROM review_return_records WHERE paper_id = ? ORDER BY id ASC
            """, [d['id']]).fetchall()
            d['return_history'] = [{
                'id': h['id'],
                'return_code': h['return_code'],
                'return_reason': h['return_reason'],
                'return_reason_type': h['return_reason_type'],
                'return_reason_type_name': RETURN_REASON_TYPE_MAP.get(h['return_reason_type'], h['return_reason_type']),
                'handling_opinion': h['handling_opinion'],
                'return_round': h['return_round'],
                'status': h['status'],
                'status_name': RETURN_STATUS_MAP.get(h['status'], h['status']),
                'created_at': str(h['created_at']) if h['created_at'] else None,
                'reevaluated_at': str(h['reevaluated_at']) if h['reevaluated_at'] else None,
                'closed_at': str(h['closed_at']) if h['closed_at'] else None,
            } for h in return_history]
        if d.get('latest_return_reason_type'):
            d['latest_return_reason_type_name'] = RETURN_REASON_TYPE_MAP.get(
                d['latest_return_reason_type'], d['latest_return_reason_type'])

        init_r = db.execute("""
            SELECT ri.initial_score, ri.deduction_reason, ri.difficulty_flag,
                   ri.completion_note, u.real_name as reviewer_name,
                   u.username as reviewer_username, t.assigned_at as review_assigned_at
            FROM reviews ri
            LEFT JOIN users u ON ri.reviewer_id = u.id
            LEFT JOIN tasks t ON ri.task_id = t.id
            WHERE ri.paper_id = ? AND ri.review_type = 'initial'
            ORDER BY ri.id DESC LIMIT 1
        """, [d['id']]).fetchone()
        audit_r = db.execute("""
            SELECT ra.audit_score, ra.final_score, ra.diff_reason, ra.handling_opinion,
                   u.real_name as auditor_name, u.username as auditor_username,
                   t.assigned_at as audit_assigned_at
            FROM reviews ra
            LEFT JOIN users u ON ra.reviewer_id = u.id
            LEFT JOIN tasks t ON ra.task_id = t.id
            WHERE ra.paper_id = ? AND ra.review_type = 'audit'
            ORDER BY ra.id DESC LIMIT 1
        """, [d['id']]).fetchone()

        if init_r:
            d['initial_review'] = dict(init_r)
        if audit_r:
            d['audit_review'] = dict(audit_r)
        if init_r and audit_r and init_r['initial_score'] is not None and audit_r['audit_score'] is not None:
            d['score_diff'] = round(abs(float(init_r['initial_score']) - float(audit_r['audit_score'])), 2)
        if audit_r and audit_r['final_score'] is not None:
            d['final_score'] = audit_r['final_score']
        elif init_r and init_r['initial_score'] is not None:
            d['final_score'] = init_r['initial_score']

        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@query_bp.route('/tasks', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def query_tasks():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    status = request.args.get('status')
    task_type = request.args.get('task_type')
    assignee_id = request.args.get('assignee_id', type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    base_query = """
        FROM tasks t
        LEFT JOIN papers p ON t.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users u ON t.assignee_id = u.id
        LEFT JOIN responsibility_groups rg ON t.group_id = rg.id
        LEFT JOIN review_appeals ra ON t.appeal_id = ra.id
        WHERE 1=1
    """
    params = []

    if batch_id:
        base_query += " AND p.batch_id = ?"
        params.append(batch_id)
    if question_group_id:
        base_query += " AND p.question_group_id = ?"
        params.append(question_group_id)
    if status:
        base_query += " AND t.status = ?"
        params.append(status)
    if task_type:
        base_query += " AND t.task_type = ?"
        params.append(task_type)
    if assignee_id:
        base_query += " AND t.assignee_id = ?"
        params.append(assignee_id)
    if date_from:
        base_query += " AND t.created_at >= ?"
        params.append(date_from)
    if date_to:
        base_query += " AND t.created_at <= ?"
        params.append(date_to + " 23:59:59")

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT t.*, p.paper_number, p.candidate_name, p.current_status as paper_status,
               p.is_reviewing, p.appeal_count,
               b.batch_name, qg.group_name as question_group_name,
               u.real_name as assignee_name, u.username as assignee_username, u.role as assignee_role,
               rg.group_name as responsibility_group,
               ra.id as appeal_id, ra.status as appeal_status, ra.appeal_type
        {base_query}
        ORDER BY t.id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['status_name'] = STATUS_MAP.get(d['status'], d['status'])
        d['paper_status_name'] = STATUS_MAP.get(d['paper_status'], d['paper_status'])
        d['assignee_role_name'] = ROLE_MAP.get(d['assignee_role'], d['assignee_role']) if d['assignee_role'] else None
        if d.get('appeal_status'):
            d['appeal_status_name'] = APPEAL_STATUS_MAP.get(d['appeal_status'], d['appeal_status'])
        if d.get('appeal_type'):
            d['appeal_type_name'] = APPEAL_TYPE_MAP.get(d['appeal_type'], d['appeal_type'])

        if d.get('return_record_id'):
            ret = db.execute("""
                SELECT rr.return_code, rr.return_reason, rr.return_reason_type,
                       rr.handling_opinion, rr.return_round, rr.status as return_status
                FROM review_return_records rr
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
                }

        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@query_bp.route('/score-diff-ranking', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def score_diff_ranking():
    db = get_db()
    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    limit = request.args.get('limit', 100, type=int)
    diff_min = request.args.get('diff_min', type=float)

    where_conds = [
        "ri.review_type = 'initial'",
        "ra.review_type = 'audit'",
        "ri.initial_score IS NOT NULL",
        "ra.audit_score IS NOT NULL"
    ]
    params = []

    if batch_id:
        where_conds.append("p.batch_id = ?")
        params.append(batch_id)
    if question_group_id:
        where_conds.append("p.question_group_id = ?")
        params.append(question_group_id)
    if diff_min is not None:
        where_conds.append("ABS(ri.initial_score - ra.audit_score) >= ?")
        params.append(diff_min)

    where_sql = " AND ".join(where_conds)

    query = f"""
        SELECT
            p.id as paper_id, p.paper_number, p.candidate_name, p.current_status,
            b.batch_name, qg.group_name as question_group_name, qg.max_score,
            ri.initial_score, ra.audit_score,
            ABS(ri.initial_score - ra.audit_score) as score_diff,
            CASE WHEN ra.final_score IS NOT NULL THEN ra.final_score
                 ELSE NULL END as final_score,
            u1.real_name as reviewer_name,
            u2.real_name as auditor_name,
            ra.diff_reason, ra.handling_opinion
        FROM reviews ri
        JOIN reviews ra ON ri.paper_id = ra.paper_id
        JOIN papers p ON ri.paper_id = p.id
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN users u1 ON ri.reviewer_id = u1.id
        LEFT JOIN users u2 ON ra.reviewer_id = u2.id
        WHERE {where_sql}
        ORDER BY score_diff DESC
        LIMIT ?
    """
    params.append(limit)
    rows = db.execute(query, params).fetchall()

    result = []
    for idx, r in enumerate(rows):
        d = dict(r)
        d['rank'] = idx + 1
        d['status_name'] = STATUS_MAP.get(d['current_status'], d['current_status'])
        d['score_diff'] = round(float(d['score_diff']), 2) if d['score_diff'] is not None else None
        d['diff_percent'] = round(d['score_diff'] / float(d['max_score']) * 100, 2) if d['score_diff'] is not None and d['max_score'] else None
        result.append(d)

    summary = db.execute(f"""
        SELECT
            COUNT(*) as total_with_diff,
            AVG(ABS(ri.initial_score - ra.audit_score)) as avg_diff,
            MAX(ABS(ri.initial_score - ra.audit_score)) as max_diff,
            SUM(CASE WHEN ABS(ri.initial_score - ra.audit_score) >= 5 THEN 1 ELSE 0 END) as over_threshold_count
        FROM reviews ri
        JOIN reviews ra ON ri.paper_id = ra.paper_id
        JOIN papers p ON ri.paper_id = p.id
        WHERE {where_sql}
    """, params[:-1]).fetchone()

    return jsonify({
        "summary": dict(summary),
        "ranking": result
    }), 200


@query_bp.route('/pending-audit-list', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def pending_audit_list():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    sort_by = request.args.get('sort_by', 'deadline')

    base_where = """
        WHERE p.current_status IN ('pending_audit', 'diff_pending')
    """
    params = []

    if batch_id:
        base_where += " AND p.batch_id = ?"
        params.append(batch_id)
    if question_group_id:
        base_where += " AND p.question_group_id = ?"
        params.append(question_group_id)

    count_sql = f"""
        SELECT COUNT(*)
        FROM papers p
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        {base_where}
    """
    count = db.execute(count_sql, params).fetchval()

    order_sql = "t.deadline_at ASC NULLS LAST"
    if sort_by == 'assigned':
        order_sql = "t.assigned_at ASC NULLS FIRST"
    elif sort_by == 'status':
        order_sql = "CASE WHEN p.current_status = 'diff_pending' THEN 0 ELSE 1 END, t.deadline_at ASC NULLS LAST"

    query = f"""
        SELECT
            t.id as task_id, t.task_code,
            CASE WHEN t.id IS NOT NULL THEN t.status ELSE p.current_status END as status,
            t.assigned_at, t.started_at, t.deadline_at,
            p.id as paper_id, p.paper_number, p.candidate_name, p.candidate_id,
            b.batch_name, qg.group_name as question_group_name, qg.max_score,
            u.real_name as assignee_name,
            CASE WHEN t.deadline_at IS NOT NULL AND t.deadline_at < LOCALTIMESTAMP
                 THEN 'timeout'
                 WHEN t.deadline_at IS NOT NULL AND t.deadline_at < (LOCALTIMESTAMP + INTERVAL 4 HOUR)
                 THEN 'urgent'
                 ELSE 'normal'
            END as urgency,
            p.current_status as paper_status
        FROM papers p
        LEFT JOIN batches b ON p.batch_id = b.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        LEFT JOIN LATERAL (
            SELECT t0.* FROM tasks t0
            WHERE t0.paper_id = p.id AND t0.task_type = 'audit' AND t0.is_active = true
            ORDER BY t0.id DESC LIMIT 1
        ) t ON true
        LEFT JOIN users u ON t.assignee_id = u.id
        {base_where}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        status_val = d['status']
        d['status_name'] = STATUS_MAP.get(status_val, status_val)

        init_r = db.execute("""
            SELECT initial_score, difficulty_flag, completion_note,
                   u.real_name as reviewer_name
            FROM reviews r
            LEFT JOIN users u ON r.reviewer_id = u.id
            WHERE r.paper_id = ? AND r.review_type = 'initial'
            ORDER BY r.id DESC LIMIT 1
        """, [d['paper_id']]).fetchone()
        if init_r:
            d['initial_review'] = dict(init_r)
            d['initial_score'] = init_r['initial_score']

        deadline_val = d.get('deadline_at')
        if deadline_val:
            if isinstance(deadline_val, str):
                deadline_dt = datetime.fromisoformat(deadline_val)
            else:
                deadline_dt = deadline_val
            d['hours_remaining'] = round((deadline_dt - datetime.now()).total_seconds() / 3600, 1)

        result.append(d)

    urgency_params = []
    urgency_where = "WHERE p.current_status IN ('pending_audit', 'diff_pending')"
    if batch_id:
        urgency_where += " AND p.batch_id = ?"
        urgency_params.append(batch_id)
    if question_group_id:
        urgency_where += " AND p.question_group_id = ?"
        urgency_params.append(question_group_id)

    urgency_count = db.execute(f"""
        SELECT
            COALESCE(SUM(CASE WHEN p.current_status = 'diff_pending' THEN 1 ELSE 0 END), 0) as diff_pending_count,
            COALESCE(SUM(CASE WHEN t.deadline_at IS NOT NULL AND t.deadline_at < LOCALTIMESTAMP THEN 1 ELSE 0 END), 0) as timeout_count,
            COALESCE(SUM(CASE WHEN t.deadline_at IS NOT NULL AND t.deadline_at >= LOCALTIMESTAMP
                      AND t.deadline_at < (LOCALTIMESTAMP + INTERVAL 4 HOUR) THEN 1 ELSE 0 END), 0) as urgent_count,
            COALESCE(SUM(CASE WHEN t.assignee_id IS NULL THEN 1 ELSE 0 END), 0) as unassigned_count
        FROM papers p
        LEFT JOIN LATERAL (
            SELECT t0.* FROM tasks t0
            WHERE t0.paper_id = p.id AND t0.task_type = 'audit' AND t0.is_active = true
            ORDER BY t0.id DESC LIMIT 1
        ) t ON true
        {urgency_where}
    """, urgency_params).fetchone()

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "urgency_summary": dict(urgency_count),
        "items": result
    }), 200


@query_bp.route('/difficulty-trend', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def difficulty_trend():
    db = get_db()
    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    days = request.args.get('days', 30, type=int)

    where_conds = ["r.difficulty_flag = true", f"r.created_at >= CURRENT_DATE - INTERVAL {days} DAY"]
    params = []

    if batch_id:
        where_conds.append("p.batch_id = ?")
        params.append(batch_id)
    if question_group_id:
        where_conds.append("p.question_group_id = ?")
        params.append(question_group_id)

    where_sql = " AND ".join(where_conds)

    by_group = db.execute(f"""
        SELECT
            qg.id as question_group_id, qg.group_code, qg.group_name,
            COUNT(*) as difficulty_count,
            SUM(CASE WHEN r.difficulty_note IS NOT NULL AND r.difficulty_note != '' THEN 1 ELSE 0 END) as with_note_count
        FROM reviews r
        JOIN papers p ON r.paper_id = p.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        WHERE {where_sql}
        GROUP BY qg.id, qg.group_code, qg.group_name
        ORDER BY difficulty_count DESC
    """, params).fetchall()

    date_series = db.execute(f"""
        SELECT
            CAST(r.created_at AS DATE) as stat_date,
            qg.id as question_group_id, qg.group_name,
            COUNT(*) as count
        FROM reviews r
        JOIN papers p ON r.paper_id = p.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        WHERE {where_sql}
        GROUP BY CAST(r.created_at AS DATE), qg.id, qg.group_name
        ORDER BY stat_date ASC, count DESC
    """, params).fetchall()

    trend_data = {}
    all_dates = set()
    groups_set = {}

    for row in date_series:
        date = str(row['stat_date'])
        all_dates.add(date)
        gid = row['question_group_id']
        if gid:
            groups_set[gid] = row['group_name']
        if date not in trend_data:
            trend_data[date] = {}
        if gid:
            trend_data[date][gid] = row['count']

    sorted_dates = sorted(all_dates)

    series_list = []
    for gid, gname in groups_set.items():
        data = []
        for d in sorted_dates:
            data.append(trend_data.get(d, {}).get(gid, 0))
        series_list.append({
            "question_group_id": gid,
            "group_name": gname,
            "data": data
        })

    by_reviewer = db.execute(f"""
        SELECT
            u.id as reviewer_id, u.real_name, u.username,
            COUNT(*) as difficulty_submitted,
            qg.group_name
        FROM reviews r
        JOIN papers p ON r.paper_id = p.id
        LEFT JOIN users u ON r.reviewer_id = u.id
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        WHERE {where_sql}
        GROUP BY u.id, u.real_name, u.username, qg.group_name
        ORDER BY difficulty_submitted DESC
        LIMIT 20
    """, params).fetchall()

    return jsonify({
        "period_days": days,
        "dates": sorted_dates,
        "by_question_group": rows_to_list(by_group),
        "by_date_series": series_list,
        "top_reviewers": rows_to_list(by_reviewer)
    }), 200


@query_bp.route('/statistics/summary', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def statistics_summary():
    db = get_db()
    batch_id = request.args.get('batch_id', type=int)

    where_cond = ""
    params = []
    if batch_id:
        where_cond = "WHERE p.batch_id = ?"
        params.append(batch_id)

    papers_total = db.execute(f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN p.current_status = 'pending_assignment' THEN 1 ELSE 0 END) as pending_assignment,
            SUM(CASE WHEN p.current_status = 'reviewing' THEN 1 ELSE 0 END) as reviewing,
            SUM(CASE WHEN p.current_status = 'pending_audit' THEN 1 ELSE 0 END) as pending_audit,
            SUM(CASE WHEN p.current_status = 'diff_pending' THEN 1 ELSE 0 END) as diff_pending,
            SUM(CASE WHEN p.current_status = 'finalized' THEN 1 ELSE 0 END) as finalized,
            SUM(CASE WHEN p.current_status = 'suspended' THEN 1 ELSE 0 END) as suspended,
            SUM(CASE WHEN p.current_status = 'pending_reeval' THEN 1 ELSE 0 END) as pending_reeval
        FROM papers p
        {where_cond}
    """, params).fetchone()

    scoring_stats = db.execute(f"""
        SELECT
            COUNT(*) as scored_count,
            AVG(CAST(ri.initial_score AS FLOAT)) as avg_initial_score,
            AVG(CAST(ra.audit_score AS FLOAT)) as avg_audit_score,
            AVG(CAST(ra.final_score AS FLOAT)) as avg_final_score,
            MIN(CAST(ra.final_score AS FLOAT)) as min_final_score,
            MAX(CAST(ra.final_score AS FLOAT)) as max_final_score,
            AVG(ABS(CAST(ri.initial_score AS FLOAT) - CAST(ra.audit_score AS FLOAT))) as avg_score_diff,
            SUM(CASE WHEN CAST(ra.final_score AS FLOAT) >= COALESCE(qg.pass_score, 60) THEN 1 ELSE 0 END) as pass_count
        FROM papers p
        LEFT JOIN reviews ri ON ri.paper_id = p.id AND ri.review_type = 'initial'
        LEFT JOIN reviews ra ON ra.paper_id = p.id AND ra.review_type = 'audit'
        LEFT JOIN question_groups qg ON p.question_group_id = qg.id
        {where_cond if where_cond else 'WHERE 1=1'}
          AND ra.final_score IS NOT NULL
    """, params).fetchone()

    workload_params = list(params)
    workload_where = 'WHERE 1=1'
    if batch_id:
        workload_where += ' AND EXISTS (SELECT 1 FROM papers pp WHERE pp.id = t.paper_id AND pp.batch_id = ?)'
    workload = db.execute(f"""
        SELECT
            u.id, u.real_name, u.username, u.role,
            SUM(CASE WHEN t.task_type = 'review' THEN 1 ELSE 0 END) as review_tasks,
            SUM(CASE WHEN t.task_type = 'audit' THEN 1 ELSE 0 END) as audit_tasks,
            SUM(CASE WHEN t.status = 'finalized' THEN 1 ELSE 0 END) as completed_tasks,
            SUM(CASE WHEN t.status IN ('reviewing', 'pending_audit', 'diff_pending') THEN 1 ELSE 0 END) as in_progress_tasks
        FROM users u
        LEFT JOIN tasks t ON t.assignee_id = u.id
        {workload_where}
        GROUP BY u.id, u.real_name, u.username, u.role
        HAVING (review_tasks + audit_tasks) > 0
        ORDER BY completed_tasks DESC
    """, workload_params).fetchall()

    return jsonify({
        "paper_status_distribution": dict(papers_total),
        "scoring_statistics": dict(scoring_stats),
        "workload_by_user": rows_to_list(workload)
    }), 200
