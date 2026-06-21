from flask import Blueprint, request, jsonify
import json
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db
from app.utils import role_required, get_current_user, row_to_dict, rows_to_list, ROLE_MAP

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    db = get_db()
    user = db.execute(
        "SELECT id, username, password_hash, role, real_name, group_id, is_active FROM users WHERE username = ?",
        [username]
    ).fetchone()

    if not user:
        return jsonify({"error": "用户名或密码错误"}), 401

    if not user['is_active']:
        return jsonify({"error": "账户已被禁用"}), 403

    if not check_password_hash(user['password_hash'], password):
        return jsonify({"error": "用户名或密码错误"}), 401

    access_token = create_access_token(
        identity=json.dumps({
            "user_id": user['id'],
            "username": user['username'],
            "role": user['role']
        })
    )

    return jsonify({
        "access_token": access_token,
        "user": {
            "id": user['id'],
            "username": user['username'],
            "role": user['role'],
            "role_name": ROLE_MAP.get(user['role'], user['role']),
            "real_name": user['real_name'],
            "group_id": user['group_id']
        }
    }), 200


@auth_bp.route('/profile', methods=['GET'])
@jwt_required()
def profile():
    user = get_current_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    user['role_name'] = ROLE_MAP.get(user['role'], user['role'])
    user.pop('password_hash', None)
    return jsonify(user), 200


@auth_bp.route('/change-password', methods=['POST'])
@jwt_required()
def change_password():
    data = request.get_json()
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')

    if not old_password or not new_password:
        return jsonify({"error": "旧密码和新密码不能为空"}), 400

    if len(new_password) < 6:
        return jsonify({"error": "新密码长度不能少于6位"}), 400

    identity_raw = get_jwt_identity()
    identity = json.loads(identity_raw) if isinstance(identity_raw, str) else identity_raw
    db = get_db()
    user = db.execute(
        "SELECT id, password_hash FROM users WHERE id = ?",
        [identity['user_id']]
    ).fetchone()

    if not check_password_hash(user['password_hash'], old_password):
        return jsonify({"error": "旧密码错误"}), 400

    new_hash = generate_password_hash(new_password)
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        [new_hash, identity['user_id']]
    )
    db.commit()

    return jsonify({"message": "密码修改成功"}), 200
