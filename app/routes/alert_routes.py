from datetime import datetime
from flask import Blueprint, request, jsonify

from app.db import get_db
from app.utils import role_required, rows_to_list, STATUS_MAP, detect_anomalies

alert_bp = Blueprint('alerts', __name__, url_prefix='/api/alerts')

ALERT_TYPE_MAP = {
    'score_diff': '评分分差过大',
    'timeout': '阅卷超时',
    'difficulty_cluster': '题型疑难集中',
    'unfinalized_after_audit': '复核后未定分',
    'backlog': '责任组处理积压',
    'review_appeal': '复评申请提醒',
    'return_reeval': '退回重评提醒',
    'task_supervision': '任务督办提醒'
}

ALERT_LEVEL_MAP = {
    'info': '提示',
    'warning': '警告',
    'critical': '严重'
}


@alert_bp.route('', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def list_alerts():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    alert_type = request.args.get('alert_type')
    alert_level = request.args.get('alert_level')
    is_handled = request.args.get('is_handled')

    base_query = """
        FROM alerts a
        LEFT JOIN papers p ON a.paper_id = p.id
        LEFT JOIN tasks t ON a.task_id = t.id
        LEFT JOIN question_groups qg ON a.question_group_id = qg.id
        LEFT JOIN responsibility_groups rg ON a.group_id = rg.id
        LEFT JOIN review_appeals ra ON a.paper_id = ra.paper_id AND ra.status IN ('pending', 'accepted', 'reviewing')
        WHERE 1=1
    """
    params = []

    if alert_type:
        base_query += " AND a.alert_type = ?"
        params.append(alert_type)
    if alert_level:
        base_query += " AND a.alert_level = ?"
        params.append(alert_level)
    if is_handled is not None and is_handled != '':
        base_query += " AND a.is_handled = ?"
        params.append(is_handled.lower() == 'true')

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT a.*, p.paper_number, p.is_reviewing, p.appeal_count,
               t.task_code, t.status as task_status, t.is_review_task, t.appeal_id as task_appeal_id,
               qg.group_name as question_group_name,
               rg.group_name as responsibility_group,
               ra.id as appeal_id, ra.status as appeal_status, ra.appeal_type, ra.priority
        {base_query}
        ORDER BY CASE WHEN a.is_handled = false THEN 0 ELSE 1 END,
                 CASE a.alert_level
                     WHEN 'critical' THEN 0
                     WHEN 'warning' THEN 1
                     ELSE 2 END,
                 a.created_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([per_page, (page - 1) * per_page])
    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['alert_type_name'] = ALERT_TYPE_MAP.get(d['alert_type'], d['alert_type'])
        d['alert_level_name'] = ALERT_LEVEL_MAP.get(d['alert_level'], d['alert_level'])
        if d.get('task_status'):
            d['task_status_name'] = STATUS_MAP.get(d['task_status'], d['task_status'])
        if d.get('appeal_status'):
            from app.utils import APPEAL_STATUS_MAP, APPEAL_TYPE_MAP
            d['appeal_status_name'] = APPEAL_STATUS_MAP.get(d['appeal_status'], d['appeal_status'])
            d['appeal_type_name'] = APPEAL_TYPE_MAP.get(d['appeal_type'], d['appeal_type'])
        result.append(d)

    stats = db.execute(f"""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN a.is_handled = false THEN 1 ELSE 0 END), 0) as unhandled,
            COALESCE(SUM(CASE WHEN a.alert_type = 'score_diff' AND a.is_handled = false THEN 1 ELSE 0 END), 0) as score_diff_count,
            COALESCE(SUM(CASE WHEN a.alert_type = 'timeout' AND a.is_handled = false THEN 1 ELSE 0 END), 0) as timeout_count,
            COALESCE(SUM(CASE WHEN a.alert_type = 'difficulty_cluster' AND a.is_handled = false THEN 1 ELSE 0 END), 0) as difficulty_count,
            COALESCE(SUM(CASE WHEN a.alert_type = 'unfinalized_after_audit' AND a.is_handled = false THEN 1 ELSE 0 END), 0) as unfinalized_count,
            COALESCE(SUM(CASE WHEN a.alert_type = 'backlog' AND a.is_handled = false THEN 1 ELSE 0 END), 0) as backlog_count,
            COALESCE(SUM(CASE WHEN a.alert_type = 'review_appeal' AND a.is_handled = false THEN 1 ELSE 0 END), 0) as review_appeal_count,
            COALESCE(SUM(CASE WHEN a.alert_type = 'return_reeval' AND a.is_handled = false THEN 1 ELSE 0 END), 0) as return_reeval_count
        {base_query}
    """, params[:-2]).fetchone()

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "statistics": dict(stats),
        "items": result
    }), 200


@alert_bp.route('/<int:alert_id>/handle', methods=['POST'])
@role_required('admin')
def handle_alert(alert_id):
    data = request.get_json() or {}
    note = data.get('handling_note', '')

    db = get_db()
    alert = db.execute("SELECT id, is_handled FROM alerts WHERE id = ?", [alert_id]).fetchone()
    if not alert:
        return jsonify({"error": "告警不存在"}), 404
    if alert['is_handled']:
        return jsonify({"error": "告警已处理，无需重复处理"}), 400

    db.execute("""
        UPDATE alerts SET is_handled = true, handled_at = ?, message = CONCAT(message, ?)
        WHERE id = ?
    """, [datetime.now(), f" [处理备注: {note}]" if note else "", alert_id])
    db.commit()

    return jsonify({"message": "告警已处理"}), 200


@alert_bp.route('/handle-batch', methods=['POST'])
@role_required('admin')
def handle_batch():
    data = request.get_json()
    alert_ids = data.get('alert_ids', [])
    alert_type = data.get('alert_type')

    db = get_db()
    now = datetime.now()

    if alert_ids:
        placeholders = ','.join(['?'] * len(alert_ids))
        db.execute(f"""
            UPDATE alerts SET is_handled = true, handled_at = ?
            WHERE id IN ({placeholders}) AND is_handled = false
        """, [now] + alert_ids)
    elif alert_type:
        db.execute("""
            UPDATE alerts SET is_handled = true, handled_at = ?
            WHERE alert_type = ? AND is_handled = false
        """, [now, alert_type])
    else:
        return jsonify({"error": "alert_ids 或 alert_type 必填其一"}), 400

    db.commit()
    updated = db.execute("SELECT CHANGES()").fetchval()

    return jsonify({"message": f"批量处理完成，共处理{updated}条告警"}), 200


@alert_bp.route('/summary', methods=['GET'])
@role_required('admin', 'reviewer', 'auditor')
def summary():
    db = get_db()

    by_type = db.execute("""
        SELECT alert_type, alert_level,
               COUNT(*) as total,
               SUM(CASE WHEN is_handled = false THEN 1 ELSE 0 END) as unhandled
        FROM alerts
        GROUP BY alert_type, alert_level
        ORDER BY alert_type, alert_level
    """).fetchall()

    result = []
    for r in by_type:
        d = dict(r)
        d['alert_type_name'] = ALERT_TYPE_MAP.get(d['alert_type'], d['alert_type'])
        d['alert_level_name'] = ALERT_LEVEL_MAP.get(d['alert_level'], d['alert_level'])
        result.append(d)

    appeal_stats = db.execute("""
        SELECT
            COUNT(*) as total_appeals,
            COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) as pending_appeals,
            COALESCE(SUM(CASE WHEN status = 'reviewing' THEN 1 ELSE 0 END), 0) as reviewing_appeals,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) as completed_appeals,
            COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0) as rejected_appeals,
            COALESCE(SUM(CASE WHEN priority = 'high' AND status IN ('pending', 'accepted', 'reviewing') THEN 1 ELSE 0 END), 0) as high_priority_pending
        FROM review_appeals
    """).fetchone()

    recent = db.execute("""
        SELECT a.*, p.paper_number, qg.group_name as question_group_name,
               p.is_reviewing, p.appeal_count
        FROM alerts a
        LEFT JOIN papers p ON a.paper_id = p.id
        LEFT JOIN question_groups qg ON a.question_group_id = qg.id
        WHERE a.is_handled = false
        ORDER BY a.created_at DESC
        LIMIT 10
    """).fetchall()

    recent_result = []
    for r in recent:
        d = dict(r)
        d['alert_type_name'] = ALERT_TYPE_MAP.get(d['alert_type'], d['alert_type'])
        d['alert_level_name'] = ALERT_LEVEL_MAP.get(d['alert_level'], d['alert_level'])
        recent_result.append(d)

    return jsonify({
        "by_type_level": result,
        "recent_unhandled": recent_result,
        "review_appeal_stats": dict(appeal_stats)
    }), 200


@alert_bp.route('/detect-anomalies', methods=['POST'])
@role_required('admin')
def trigger_detect():
    db = get_db()
    alerts = detect_anomalies(db)
    return jsonify({
        "message": "异常检测完成",
        "new_alerts_count": len(alerts),
        "alerts": alerts
    }), 200
