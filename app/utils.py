from datetime import datetime, timedelta
from flask import jsonify, request
from functools import wraps
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from app.db import get_db
from config import Config


def role_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        def decorator(*args, **kwargs):
            verify_jwt_in_request()
            identity = get_jwt_identity()
            db = get_db()
            user = db.execute(
                "SELECT id, role, is_active FROM users WHERE id = ?",
                [identity['user_id']]
            ).fetchone()
            if not user:
                return jsonify({"error": "用户不存在"}), 401
            if not user['is_active']:
                return jsonify({"error": "账户已禁用"}), 403
            if user['role'] not in roles:
                return jsonify({"error": "权限不足"}), 403
            return fn(*args, **kwargs)
        return decorator
    return wrapper


def get_current_user():
    identity = get_jwt_identity()
    db = get_db()
    user = db.execute(
        "SELECT id, username, role, real_name, group_id, is_active FROM users WHERE id = ?",
        [identity['user_id']]
    ).fetchone()
    return dict(user) if user else None


def generate_task_code(task_type):
    prefix = 'RV' if task_type == 'review' else 'AU'
    ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
    return f"{prefix}{ts}"


def ensure_single_active_task(db, paper_id, task_type):
    existing = db.execute("""
        SELECT id, status FROM tasks
        WHERE paper_id = ? AND task_type = ? AND is_active = true
    """, [paper_id, task_type]).fetchone()
    return dict(existing) if existing else None


def calculate_deadline(hours=48):
    return datetime.now() + timedelta(hours=hours)


def update_paper_status(db, paper_id, new_status):
    valid_statuses = [
        'pending_assignment', 'reviewing', 'pending_audit',
        'diff_pending', 'finalized', 'suspended'
    ]
    if new_status not in valid_statuses:
        raise ValueError(f"无效状态: {new_status}")
    db.execute(
        "UPDATE papers SET current_status = ? WHERE id = ?",
        [new_status, paper_id]
    )


def check_score_diff(initial_score, audit_score):
    if initial_score is None or audit_score is None:
        return False
    diff = abs(float(initial_score) - float(audit_score))
    return diff >= Config.SCORE_DIFF_THRESHOLD, diff


def detect_anomalies(db):
    alerts = []

    result = db.execute("""
        SELECT r_init.paper_id, r_init.initial_score, r_audit.audit_score,
               p.paper_number
        FROM reviews r_init
        JOIN reviews r_audit ON r_init.paper_id = r_audit.paper_id
        JOIN papers p ON r_init.paper_id = p.id
        WHERE r_init.review_type = 'initial'
          AND r_audit.review_type = 'audit'
          AND r_init.initial_score IS NOT NULL
          AND r_audit.audit_score IS NOT NULL
          AND r_audit.final_score IS NULL
          AND p.current_status != 'finalized'
    """).fetchall()
    for row in result:
        is_diff, diff_val = check_score_diff(row['initial_score'], row['audit_score'])
        if is_diff:
            existing = db.execute("""
                SELECT id FROM alerts WHERE alert_type='score_diff'
                  AND paper_id=? AND is_handled=false
            """, [row['paper_id']]).fetchone()
            if not existing:
                db.execute("""
                    INSERT INTO alerts (alert_type, alert_level, paper_id, message, detail_json)
                    VALUES ('score_diff', 'warning', ?, ?, ?)
                """, [
                    row['paper_id'],
                    f"试卷{row['paper_number']}评分分差{diff_val:.1f}分，超过阈值",
                    f'{{"diff": {diff_val}, "initial": {row["initial_score"]}, "audit": {row["audit_score"]}}}'
                ])
                alerts.append({'type': 'score_diff', 'paper': row['paper_number']})

    timeout_tasks = db.execute("""
        SELECT t.id, t.paper_id, t.assignee_id, t.assigned_at, t.status, t.task_type,
               p.paper_number, u.real_name,
               rg.review_time_limit_hours, rg.audit_time_limit_hours
        FROM tasks t
        JOIN papers p ON t.paper_id = p.id
        LEFT JOIN users u ON t.assignee_id = u.id
        LEFT JOIN responsibility_groups rg ON t.group_id = rg.id
        WHERE t.status IN ('reviewing', 'pending_audit')
          AND t.is_active = true
    """).fetchall()
    for t in timeout_tasks:
        limit_hours = (
            float(t['review_time_limit_hours']) if (
                t['review_time_limit_hours'] is not None
                and t['status'] == 'reviewing'
            )
            else (
                float(t['audit_time_limit_hours']) if (
                    t['audit_time_limit_hours'] is not None
                    and t['status'] == 'pending_audit'
                )
                else Config.REVIEW_TIMEOUT_HOURS
            )
        )
        timeout_dt = datetime.now() - timedelta(hours=limit_hours)
        assigned_at_dt = t['assigned_at']
        if isinstance(assigned_at_dt, str):
            try:
                assigned_at_dt = datetime.fromisoformat(assigned_at_dt)
            except Exception:
                assigned_at_dt = datetime.now()
        if assigned_at_dt and assigned_at_dt < timeout_dt:
            existing = db.execute("""
                SELECT id FROM alerts WHERE alert_type='timeout'
                  AND task_id=? AND is_handled=false
            """, [t['id']]).fetchone()
            if not existing:
                db.execute("""
                    INSERT INTO alerts (alert_type, alert_level, task_id, paper_id, message)
                    VALUES ('timeout', 'critical', ?, ?, ?)
                """, [
                    t['id'], t['paper_id'],
                    f"任务超时：{t['paper_number']}分配给{t['real_name']}已超过{limit_hours:.0f}小时"
                ])
                alerts.append({'type': 'timeout', 'task': t['id']})

    difficulty_cluster = db.execute("""
        SELECT question_group_id, COUNT(*) as cnt,
               qg.group_code, qg.group_name
        FROM reviews r
        JOIN papers p ON r.paper_id = p.id
        JOIN question_groups qg ON p.question_group_id = qg.id
        WHERE r.difficulty_flag = true
          AND r.created_at >= CURRENT_DATE - INTERVAL 7 DAY
        GROUP BY question_group_id, qg.group_code, qg.group_name
        HAVING COUNT(*) >= ?
    """, [Config.DIFFICULTY_CLUSTER_THRESHOLD]).fetchall()
    for dc in difficulty_cluster:
        existing = db.execute("""
            SELECT id FROM alerts WHERE alert_type='difficulty_cluster'
              AND question_group_id=? AND is_handled=false
        """, [dc['question_group_id']]).fetchone()
        if not existing:
            db.execute("""
                INSERT INTO alerts (alert_type, alert_level, question_group_id, message, detail_json)
                VALUES ('difficulty_cluster', 'warning', ?, ?, ?)
            """, [
                dc['question_group_id'],
                f"题型组{dc['group_name']}近7天疑难标记{dc['cnt']}份，超过阈值",
                f'{{"count": {dc["cnt"]}}}'
            ])
            alerts.append({'type': 'difficulty_cluster', 'group': dc['group_code']})

    unfinalized = db.execute("""
        SELECT r.id, r.paper_id, r.task_id, p.paper_number
        FROM reviews r
        JOIN papers p ON r.paper_id = p.id
        WHERE r.review_type = 'audit'
          AND r.audit_score IS NOT NULL
          AND r.final_score IS NULL
          AND p.current_status NOT IN ('finalized', 'suspended')
          AND r.created_at < CURRENT_DATE - INTERVAL 2 DAY
    """).fetchall()
    for u in unfinalized:
        existing = db.execute("""
            SELECT id FROM alerts WHERE alert_type='unfinalized_after_audit'
              AND paper_id=? AND is_handled=false
        """, [u['paper_id']]).fetchone()
        if not existing:
            db.execute("""
                INSERT INTO alerts (alert_type, alert_level, paper_id, task_id, message)
                VALUES ('unfinalized_after_audit', 'info', ?, ?, ?)
            """, [
                u['paper_id'], u['task_id'],
                f"试卷{u['paper_number']}复核完成超过2天未定分"
            ])
            alerts.append({'type': 'unfinalized_after_audit', 'paper': u['paper_number']})

    backlog = db.execute("""
        SELECT t.group_id, rg.group_code, rg.group_name,
               COUNT(*) as pending_count
        FROM tasks t
        JOIN responsibility_groups rg ON t.group_id = rg.id
        WHERE t.status IN ('reviewing', 'pending_audit', 'diff_pending')
          AND t.is_active = true
        GROUP BY t.group_id, rg.group_code, rg.group_name
        HAVING COUNT(*) >= ?
    """, [Config.BACKLOG_THRESHOLD]).fetchall()
    for b in backlog:
        existing = db.execute("""
            SELECT id FROM alerts WHERE alert_type='backlog'
              AND group_id=? AND is_handled=false
        """, [b['group_id']]).fetchone()
        if not existing:
            db.execute("""
                INSERT INTO alerts (alert_type, alert_level, group_id, message, detail_json)
                VALUES ('backlog', 'warning', ?, ?, ?)
            """, [
                b['group_id'],
                f"责任组{b['group_name']}待处理任务{b['pending_count']}个，出现积压",
                f'{{"pending_count": {b["pending_count"]}}}'
            ])
            alerts.append({'type': 'backlog', 'group': b['group_code']})

    db.commit()
    return alerts


def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    return [dict(r) for r in rows]


STATUS_MAP = {
    'pending_assignment': '待分配',
    'reviewing': '阅卷中',
    'pending_audit': '待复核',
    'diff_pending': '差异待处理',
    'finalized': '已定分',
    'suspended': '暂停处理',
    'returned': '已退回'
}

ROLE_MAP = {
    'admin': '管理员',
    'reviewer': '阅卷员',
    'auditor': '复核员'
}
