from datetime import datetime
from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash
from config import Config

from app.db import get_db
from app.utils import (
    role_required, get_current_user, row_to_dict, rows_to_list,
    generate_task_code, ensure_single_active_task, calculate_deadline,
    update_paper_status, STATUS_MAP, ROLE_MAP,
    APPEAL_STATUS_MAP, APPEAL_TYPE_MAP, APPEAL_PRIORITY_MAP,
    RETURN_REASON_TYPE_MAP, RETURN_STATUS_MAP
)

admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')


# -------------------- 用户管理 --------------------

@admin_bp.route('/users', methods=['GET'])
@role_required('admin')
def list_users():
    db = get_db()
    role = request.args.get('role')
    query = """
        SELECT u.id, u.username, u.role, u.real_name, u.group_id, u.created_at, u.is_active,
               rg.group_name
        FROM users u
        LEFT JOIN responsibility_groups rg ON u.group_id = rg.id
        WHERE 1=1
    """
    params = []
    if role:
        query += " AND u.role = ?"
        params.append(role)
    query += " ORDER BY u.id DESC"
    rows = db.execute(query, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['role_name'] = ROLE_MAP.get(d['role'], d['role'])
        result.append(d)
    return jsonify(result), 200


@admin_bp.route('/users', methods=['POST'])
@role_required('admin')
def create_user():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', 'reviewer123')
    role = data.get('role', 'reviewer')
    real_name = data.get('real_name', '')
    group_id = data.get('group_id')

    if not username or role not in ('admin', 'reviewer', 'auditor'):
        return jsonify({"error": "参数不完整或角色无效"}), 400

    if len(password) < 6:
        return jsonify({"error": "密码长度不能少于6位"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username = ?", [username]).fetchone()
    if existing:
        return jsonify({"error": "用户名已存在"}), 400

    password_hash = generate_password_hash(password)
    cursor = db.execute("""
        INSERT INTO users (username, password_hash, role, real_name, group_id)
        VALUES (?, ?, ?, ?, ?) RETURNING id
    """, [username, password_hash, role, real_name, group_id])
    new_id = cursor.fetchval()
    db.commit()

    return jsonify({"id": new_id, "message": "用户创建成功"}), 201


@admin_bp.route('/users/<int:user_id>', methods=['PUT'])
@role_required('admin')
def update_user(user_id):
    data = request.get_json()
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", [user_id]).fetchone()
    if not user:
        return jsonify({"error": "用户不存在"}), 404

    fields = []
    params = []
    for k in ('real_name', 'group_id', 'is_active', 'role'):
        if k in data:
            if k == 'role' and data[k] not in ('admin', 'reviewer', 'auditor'):
                continue
            fields.append(f"{k} = ?")
            params.append(data[k])
    if data.get('password'):
        fields.append("password_hash = ?")
        params.append(generate_password_hash(data['password']))

    if fields:
        params.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()

    return jsonify({"message": "用户更新成功"}), 200


# -------------------- 批次管理 --------------------

@admin_bp.route('/batches', methods=['GET'])
@role_required('admin')
def list_batches():
    db = get_db()
    rows = db.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
    return jsonify(rows_to_list(rows)), 200


@admin_bp.route('/batches', methods=['POST'])
@role_required('admin')
def create_batch():
    data = request.get_json()
    user = get_current_user()
    batch_code = data.get('batch_code', '').strip()
    batch_name = data.get('batch_name', '').strip()
    description = data.get('description', '')
    status = data.get('status', 'active')
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    if not batch_code or not batch_name:
        return jsonify({"error": "批次编号和名称必填"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM batches WHERE batch_code = ?", [batch_code]).fetchone()
    if existing:
        return jsonify({"error": "批次编号已存在"}), 400

    cursor = db.execute("""
        INSERT INTO batches (batch_code, batch_name, description, status, start_date, end_date, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
    """, [batch_code, batch_name, description, status, start_date, end_date, user['id']])
    new_id = cursor.fetchval()
    db.commit()

    return jsonify({"id": new_id, "message": "批次创建成功"}), 201


@admin_bp.route('/batches/<int:batch_id>', methods=['PUT'])
@role_required('admin')
def update_batch(batch_id):
    data = request.get_json()
    db = get_db()
    b = db.execute("SELECT id FROM batches WHERE id = ?", [batch_id]).fetchone()
    if not b:
        return jsonify({"error": "批次不存在"}), 404

    fields = []
    params = []
    for k in ('batch_name', 'description', 'status', 'start_date', 'end_date'):
        if k in data:
            fields.append(f"{k} = ?")
            params.append(data[k])
    if fields:
        params.append(batch_id)
        db.execute(f"UPDATE batches SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()

    return jsonify({"message": "批次更新成功"}), 200


@admin_bp.route('/batches/<int:batch_id>', methods=['DELETE'])
@role_required('admin')
def delete_batch(batch_id):
    db = get_db()
    has_papers = db.execute("SELECT COUNT(*) FROM papers WHERE batch_id = ?", [batch_id]).fetchval()
    if has_papers > 0:
        return jsonify({"error": "该批次下存在试卷，无法删除"}), 400
    db.execute("DELETE FROM batches WHERE id = ?", [batch_id])
    db.commit()
    return jsonify({"message": "批次删除成功"}), 200


# -------------------- 题型组管理 --------------------

@admin_bp.route('/question-groups', methods=['GET'])
@role_required('admin')
def list_question_groups():
    db = get_db()
    batch_id = request.args.get('batch_id', type=int)
    query = """
        SELECT qg.*, b.batch_name
        FROM question_groups qg
        LEFT JOIN batches b ON qg.batch_id = b.id
        WHERE 1=1
    """
    params = []
    if batch_id:
        query += " AND qg.batch_id = ?"
        params.append(batch_id)
    query += " ORDER BY qg.id DESC"
    rows = db.execute(query, params).fetchall()
    return jsonify(rows_to_list(rows)), 200


@admin_bp.route('/question-groups', methods=['POST'])
@role_required('admin')
def create_question_group():
    data = request.get_json()
    group_code = data.get('group_code', '').strip()
    group_name = data.get('group_name', '').strip()
    description = data.get('description', '')
    batch_id = data.get('batch_id')
    max_score = data.get('max_score', 100)
    pass_score = data.get('pass_score', 60)

    if not group_code or not group_name:
        return jsonify({"error": "题型组编号和名称必填"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM question_groups WHERE group_code = ?", [group_code]).fetchone()
    if existing:
        return jsonify({"error": "题型组编号已存在"}), 400

    cursor = db.execute("""
        INSERT INTO question_groups (group_code, group_name, description, batch_id, max_score, pass_score)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING id
    """, [group_code, group_name, description, batch_id, max_score, pass_score])
    new_id = cursor.fetchval()
    db.commit()

    return jsonify({"id": new_id, "message": "题型组创建成功"}), 201


@admin_bp.route('/question-groups/<int:qg_id>', methods=['PUT'])
@role_required('admin')
def update_question_group(qg_id):
    data = request.get_json()
    db = get_db()
    q = db.execute("SELECT id FROM question_groups WHERE id = ?", [qg_id]).fetchone()
    if not q:
        return jsonify({"error": "题型组不存在"}), 404

    fields = []
    params = []
    for k in ('group_name', 'description', 'batch_id', 'max_score', 'pass_score'):
        if k in data:
            fields.append(f"{k} = ?")
            params.append(data[k])
    if fields:
        params.append(qg_id)
        db.execute(f"UPDATE question_groups SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()

    return jsonify({"message": "题型组更新成功"}), 200


# -------------------- 试卷管理 --------------------

@admin_bp.route('/papers', methods=['GET'])
@role_required('admin')
def list_papers():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    batch_id = request.args.get('batch_id', type=int)
    question_group_id = request.args.get('question_group_id', type=int)
    status = request.args.get('status')

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

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT p.*, b.batch_name, qg.group_name as question_group_name,
               ra.id as current_appeal_id, ra.status as appeal_status,
               ra.appeal_type, ra.priority
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
        result.append(d)

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "items": result
    }), 200


@admin_bp.route('/papers', methods=['POST'])
@role_required('admin')
def create_paper():
    data = request.get_json()
    paper_number = data.get('paper_number', '').strip()
    batch_id = data.get('batch_id')
    question_group_id = data.get('question_group_id')
    candidate_name = data.get('candidate_name', '')
    candidate_id = data.get('candidate_id', '')
    paper_content = data.get('paper_content', '')
    storage_path = data.get('storage_path', '')

    if not paper_number or not batch_id or not question_group_id:
        return jsonify({"error": "试卷编号、批次、题型组为必填项"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM papers WHERE paper_number = ?", [paper_number]).fetchone()
    if existing:
        return jsonify({"error": "试卷编号已存在"}), 400

    cursor = db.execute("""
        INSERT INTO papers (paper_number, batch_id, question_group_id, candidate_name, candidate_id, paper_content, storage_path)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
    """, [paper_number, batch_id, question_group_id, candidate_name, candidate_id, paper_content, storage_path])
    new_id = cursor.fetchval()
    db.commit()

    return jsonify({"id": new_id, "message": "试卷创建成功"}), 201


@admin_bp.route('/papers/bulk', methods=['POST'])
@role_required('admin')
def bulk_create_papers():
    data = request.get_json()
    papers = data.get('papers', [])
    if not papers:
        return jsonify({"error": "试卷列表不能为空"}), 400

    db = get_db()
    created = 0
    skipped = 0
    for p in papers:
        existing = db.execute("SELECT id FROM papers WHERE paper_number = ?", [p.get('paper_number', '')]).fetchone()
        if existing:
            skipped += 1
            continue
        db.execute("""
            INSERT INTO papers (paper_number, batch_id, question_group_id, candidate_name, candidate_id)
            VALUES (?, ?, ?, ?, ?)
        """, [
            p.get('paper_number', ''),
            p.get('batch_id'),
            p.get('question_group_id'),
            p.get('candidate_name', ''),
            p.get('candidate_id', '')
        ])
        created += 1
    db.commit()

    return jsonify({"created": created, "skipped": skipped, "message": f"成功创建{created}份试卷"}), 201


@admin_bp.route('/papers/<int:paper_id>', methods=['PUT'])
@role_required('admin')
def update_paper(paper_id):
    data = request.get_json()
    db = get_db()
    p = db.execute("SELECT id FROM papers WHERE id = ?", [paper_id]).fetchone()
    if not p:
        return jsonify({"error": "试卷不存在"}), 404

    fields = []
    params = []
    for k in ('candidate_name', 'candidate_id', 'paper_content', 'storage_path', 'current_status'):
        if k in data:
            if k == 'current_status' and data[k] not in STATUS_MAP:
                continue
            fields.append(f"{k} = ?")
            params.append(data[k])
    if fields:
        params.append(paper_id)
        db.execute(f"UPDATE papers SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()

    return jsonify({"message": "试卷更新成功"}), 200


# -------------------- 评分规则 --------------------

@admin_bp.route('/scoring-rules', methods=['GET'])
@role_required('admin')
def list_scoring_rules():
    db = get_db()
    question_group_id = request.args.get('question_group_id', type=int)
    query = """
        SELECT sr.*, qg.group_name
        FROM scoring_rules sr
        LEFT JOIN question_groups qg ON sr.question_group_id = qg.id
        WHERE 1=1
    """
    params = []
    if question_group_id:
        query += " AND sr.question_group_id = ?"
        params.append(question_group_id)
    query += " ORDER BY sr.id DESC"
    rows = db.execute(query, params).fetchall()
    return jsonify(rows_to_list(rows)), 200


@admin_bp.route('/scoring-rules', methods=['POST'])
@role_required('admin')
def create_scoring_rule():
    data = request.get_json()
    rule_code = data.get('rule_code', '').strip()
    rule_name = data.get('rule_name', '').strip()
    question_group_id = data.get('question_group_id')
    description = data.get('description', '')
    criteria_json = data.get('criteria_json', '')
    score_guide = data.get('score_guide', '')
    deduction_rules = data.get('deduction_rules', '')

    if not rule_code or not rule_name:
        return jsonify({"error": "规则编号和名称必填"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM scoring_rules WHERE rule_code = ?", [rule_code]).fetchone()
    if existing:
        return jsonify({"error": "规则编号已存在"}), 400

    cursor = db.execute("""
        INSERT INTO scoring_rules (rule_code, rule_name, question_group_id, description, criteria_json, score_guide, deduction_rules)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
    """, [rule_code, rule_name, question_group_id, description, criteria_json, score_guide, deduction_rules])
    new_id = cursor.fetchval()
    db.commit()

    return jsonify({"id": new_id, "message": "评分规则创建成功"}), 201


@admin_bp.route('/scoring-rules/<int:rule_id>', methods=['PUT'])
@role_required('admin')
def update_scoring_rule(rule_id):
    data = request.get_json()
    db = get_db()
    r = db.execute("SELECT id FROM scoring_rules WHERE id = ?", [rule_id]).fetchone()
    if not r:
        return jsonify({"error": "规则不存在"}), 404

    fields = []
    params = []
    for k in ('rule_name', 'question_group_id', 'description', 'criteria_json', 'score_guide', 'deduction_rules'):
        if k in data:
            fields.append(f"{k} = ?")
            params.append(data[k])
    if fields:
        params.append(rule_id)
        db.execute(f"UPDATE scoring_rules SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()

    return jsonify({"message": "评分规则更新成功"}), 200


# -------------------- 责任组 --------------------

@admin_bp.route('/responsibility-groups', methods=['GET'])
@role_required('admin')
def list_responsibility_groups():
    db = get_db()
    rows = db.execute("""
        SELECT rg.*, b.batch_name, qg.group_name as question_group_name
        FROM responsibility_groups rg
        LEFT JOIN batches b ON rg.batch_id = b.id
        LEFT JOIN question_groups qg ON rg.question_group_id = qg.id
        ORDER BY rg.id DESC
    """).fetchall()
    return jsonify(rows_to_list(rows)), 200


@admin_bp.route('/responsibility-groups', methods=['POST'])
@role_required('admin')
def create_responsibility_group():
    data = request.get_json()
    group_code = data.get('group_code', '').strip()
    group_name = data.get('group_name', '').strip()
    description = data.get('description', '')
    batch_id = data.get('batch_id')
    question_group_id = data.get('question_group_id')
    review_time_limit_hours = data.get('review_time_limit_hours', data.get('deadline_hours', 48))
    audit_time_limit_hours = data.get('audit_time_limit_hours', data.get('deadline_hours', 24))

    if not group_code or not group_name:
        return jsonify({"error": "责任组编号和名称必填"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM responsibility_groups WHERE group_code = ?", [group_code]).fetchone()
    if existing:
        return jsonify({"error": "责任组编号已存在"}), 400

    cursor = db.execute("""
        INSERT INTO responsibility_groups (group_code, group_name, description, batch_id, question_group_id, review_time_limit_hours, audit_time_limit_hours)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
    """, [group_code, group_name, description, batch_id, question_group_id, review_time_limit_hours, audit_time_limit_hours])
    new_id = cursor.fetchval()
    db.commit()

    return jsonify({"id": new_id, "message": "责任组创建成功"}), 201


@admin_bp.route('/responsibility-groups/<int:rg_id>', methods=['PUT'])
@role_required('admin')
def update_responsibility_group(rg_id):
    data = request.get_json()
    db = get_db()
    g = db.execute("SELECT id FROM responsibility_groups WHERE id = ?", [rg_id]).fetchone()
    if not g:
        return jsonify({"error": "责任组不存在"}), 404

    fields = []
    params = []
    allowed = ('group_name', 'description', 'batch_id', 'question_group_id',
               'review_time_limit_hours', 'audit_time_limit_hours', 'deadline_hours')
    for k in allowed:
        if k in data:
            if k == 'deadline_hours':
                if 'review_time_limit_hours' not in data:
                    fields.append("review_time_limit_hours = ?")
                    params.append(data[k])
                if 'audit_time_limit_hours' not in data:
                    fields.append("audit_time_limit_hours = ?")
                    params.append(data[k])
            else:
                fields.append(f"{k} = ?")
                params.append(data[k])
    if fields:
        params.append(rg_id)
        db.execute(f"UPDATE responsibility_groups SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()

    return jsonify({"message": "责任组更新成功"}), 200


# -------------------- 任务分配 --------------------

@admin_bp.route('/tasks/assign', methods=['POST'])
@role_required('admin')
def assign_task():
    data = request.get_json()
    paper_ids = data.get('paper_ids', [])
    assignee_id = data.get('assignee_id')
    task_type = data.get('task_type', 'review')
    group_id = data.get('group_id')

    if not paper_ids or not assignee_id:
        return jsonify({"error": "试卷ID列表和阅卷人必填"}), 400
    if task_type not in ('review', 'audit'):
        return jsonify({"error": "任务类型无效"}), 400

    db = get_db()
    assignee = db.execute("SELECT id, role, group_id FROM users WHERE id = ? AND is_active = true", [assignee_id]).fetchone()
    if not assignee:
        return jsonify({"error": "阅卷人不存在或已禁用"}), 400

    if task_type == 'review' and assignee['role'] not in ('reviewer', 'admin'):
        return jsonify({"error": "阅卷任务只能分配给阅卷员或管理员"}), 400
    if task_type == 'audit' and assignee['role'] not in ('auditor', 'admin'):
        return jsonify({"error": "复核任务只能分配给复核员或管理员"}), 400

    assigned_count = 0
    skipped = []
    now = datetime.now()

    for pid in paper_ids:
        paper = db.execute("SELECT id, current_status, question_group_id FROM papers WHERE id = ?", [pid]).fetchone()
        if not paper:
            skipped.append({"paper_id": pid, "reason": "试卷不存在"})
            continue

        active_task = ensure_single_active_task(db, pid, task_type)
        if active_task:
            skipped.append({"paper_id": pid, "reason": "该试卷已有活跃任务"})
            continue

        rg = None
        final_group_id = group_id
        if not final_group_id:
            rg = db.execute("""
                SELECT id, review_time_limit_hours, audit_time_limit_hours
                FROM responsibility_groups
                WHERE question_group_id = ? AND (batch_id = (SELECT batch_id FROM papers WHERE id = ?) OR batch_id IS NULL)
                LIMIT 1
            """, [paper['question_group_id'], pid]).fetchone()
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
        if task_type == 'audit' and paper['current_status'] not in ('pending_audit', 'diff_pending', 'suspended'):
            skipped.append({"paper_id": pid, "reason": f"当前状态{paper['current_status']}不允许分配复核"})
            continue

        task_code = generate_task_code(task_type)
        cursor = db.execute("""
            INSERT INTO tasks (task_code, paper_id, task_type, assignee_id, group_id, status, assigned_at, deadline_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, true) RETURNING id
        """, [task_code, pid, task_type, assignee_id, final_group_id, new_status, now, deadline])
        task_id = cursor.fetchval()

        if task_type == 'review':
            db.execute("""
                INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type)
                VALUES (?, ?, ?, 'initial')
            """, [task_id, pid, assignee_id])

        update_paper_status(db, pid, new_status)
        assigned_count += 1

    db.commit()

    return jsonify({
        "assigned": assigned_count,
        "skipped": skipped,
        "message": f"成功分配{assigned_count}个任务"
    }), 200


@admin_bp.route('/tasks/batch-auto-assign', methods=['POST'])
@role_required('admin')
def batch_auto_assign():
    data = request.get_json()
    batch_id = data.get('batch_id')
    task_type = data.get('task_type', 'review')
    reviewer_ids = data.get('reviewer_ids', [])
    strategy = data.get('strategy', 'round_robin')

    if not batch_id or not reviewer_ids:
        return jsonify({"error": "批次和阅卷人必填"}), 400

    db = get_db()

    status_cond = "current_status = 'pending_assignment'" if task_type == 'review' else "current_status = 'pending_audit'"
    papers = db.execute(f"""
        SELECT id, question_group_id FROM papers
        WHERE batch_id = ? AND {status_cond}
    """, [batch_id]).fetchall()

    if not papers:
        return jsonify({"assigned": 0, "message": "没有可分配的试卷"}), 200

    assigned_count = 0
    now = datetime.now()
    for idx, p in enumerate(papers):
        assignee_id = reviewer_ids[idx % len(reviewer_ids)]
        active_task = ensure_single_active_task(db, p['id'], task_type)
        if active_task:
            continue

        rg = db.execute("""
            SELECT id, review_time_limit_hours, audit_time_limit_hours
            FROM responsibility_groups
            WHERE question_group_id = ? AND (batch_id = ? OR batch_id IS NULL)
            ORDER BY batch_id DESC NULLS LAST LIMIT 1
        """, [p['question_group_id'], batch_id]).fetchone()

        if rg:
            rg_id = rg['id']
            deadline_hours = (
                float(rg['review_time_limit_hours']) if task_type == 'review'
                else float(rg['audit_time_limit_hours'])
            )
        else:
            rg_id = None
            deadline_hours = Config.REVIEW_TIMEOUT_HOURS if task_type == 'review' else 24
        deadline = calculate_deadline(deadline_hours)

        new_status = 'reviewing' if task_type == 'review' else 'pending_audit'
        task_code = generate_task_code(task_type)
        cursor = db.execute("""
            INSERT INTO tasks (task_code, paper_id, task_type, assignee_id, group_id, status, assigned_at, deadline_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, true) RETURNING id
        """, [task_code, p['id'], task_type, assignee_id, rg_id, new_status, now, deadline])
        task_id = cursor.fetchval()

        if task_type == 'review':
            db.execute("""
                INSERT INTO reviews (task_id, paper_id, reviewer_id, review_type)
                VALUES (?, ?, ?, 'initial')
            """, [task_id, p['id'], assignee_id])

        update_paper_status(db, p['id'], new_status)
        assigned_count += 1

    db.commit()

    return jsonify({
        "assigned": assigned_count,
        "total_papers": len(papers),
        "message": f"自动分配{assigned_count}份试卷, 分配{assigned_count}/{len(papers)}份"
    }), 200


# -------------------- 任务管理 --------------------

@admin_bp.route('/tasks', methods=['GET'])
@role_required('admin')
def list_tasks():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    status = request.args.get('status')
    task_type = request.args.get('task_type')
    assignee_id = request.args.get('assignee_id', type=int)
    paper_id = request.args.get('paper_id', type=int)

    base_query = """
        FROM tasks t
        LEFT JOIN papers p ON t.paper_id = p.id
        LEFT JOIN users u ON t.assignee_id = u.id
        LEFT JOIN responsibility_groups rg ON t.group_id = rg.id
        LEFT JOIN review_appeals ra ON t.appeal_id = ra.id
        WHERE 1=1
    """
    params = []
    if status:
        base_query += " AND t.status = ?"
        params.append(status)
    if task_type:
        base_query += " AND t.task_type = ?"
        params.append(task_type)
    if assignee_id:
        base_query += " AND t.assignee_id = ?"
        params.append(assignee_id)
    if paper_id:
        base_query += " AND t.paper_id = ?"
        params.append(paper_id)

    count = db.execute(f"SELECT COUNT(*) {base_query}", params).fetchval()

    query = f"""
        SELECT t.*, p.paper_number, p.candidate_name, p.current_status as paper_status,
               p.is_reviewing, p.appeal_count,
               u.username as assignee_name, u.real_name as assignee_real_name,
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


@admin_bp.route('/tasks/<int:task_id>/status', methods=['PUT'])
@role_required('admin')
def update_task_status(task_id):
    data = request.get_json()
    new_status = data.get('status')
    if new_status not in STATUS_MAP and new_status not in ('returned', 'suspended', 'pending_reeval'):
        return jsonify({"error": "无效状态"}), 400

    db = get_db()
    task = db.execute("SELECT id, paper_id, task_type, status FROM tasks WHERE id = ?", [task_id]).fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    db.execute("UPDATE tasks SET status = ? WHERE id = ?", [new_status, task_id])

    if new_status == 'returned':
        db.execute("UPDATE tasks SET is_active = false WHERE id = ?", [task_id])
        back_to = 'pending_assignment' if task['task_type'] == 'review' else 'pending_audit'
        other_active = db.execute("""
            SELECT 1 FROM tasks
            WHERE paper_id = ? AND task_type = ? AND is_active = true AND id != ?
            LIMIT 1
        """, [task['paper_id'], task['task_type'], task_id]).fetchone()
        if not other_active:
            update_paper_status(db, task['paper_id'], back_to)
    elif new_status in STATUS_MAP:
        update_paper_status(db, task['paper_id'], new_status)
    db.commit()

    return jsonify({"message": "任务状态更新成功"}), 200


@admin_bp.route('/papers/<int:paper_id>/finalize', methods=['POST'])
@role_required('admin')
def finalize_paper(paper_id):
    data = request.get_json()
    final_score = data.get('final_score')
    handling_opinion = data.get('handling_opinion', '')

    if final_score is None:
        return jsonify({"error": "最终分必填"}), 400

    db = get_db()
    paper = db.execute("SELECT id, current_status FROM papers WHERE id = ?", [paper_id]).fetchone()
    if not paper:
        return jsonify({"error": "试卷不存在"}), 404

    review = db.execute("""
        SELECT id FROM reviews WHERE paper_id = ? AND review_type = 'audit'
        ORDER BY id DESC LIMIT 1
    """, [paper_id]).fetchone()

    if review:
        db.execute("""
            UPDATE reviews SET final_score = ?, handling_opinion = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, [final_score, handling_opinion, review['id']])
    else:
        db.execute("""
            INSERT INTO reviews (paper_id, review_type, final_score, handling_opinion)
            VALUES (?, 'audit', ?, ?)
        """, [paper_id, final_score, handling_opinion])

    update_paper_status(db, paper_id, 'finalized')
    db.execute("UPDATE tasks SET status = 'finalized', is_active = false, completed_at = CURRENT_TIMESTAMP WHERE paper_id = ?", [paper_id])
    db.commit()

    return jsonify({"message": "试卷已定分", "final_score": final_score}), 200
