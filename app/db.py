import duckdb
from flask import current_app, g
from config import Config


def _row_to_dict(row, description):
    if row is None:
        return None
    columns = [desc[0] for desc in description]
    return dict(zip(columns, row))


def _rows_to_list(rows, description):
    if not rows:
        return []
    columns = [desc[0] for desc in description]
    return [dict(zip(columns, r)) for r in rows]


class DuckDBWrapper:
    def __init__(self, conn):
        self._conn = conn
        self._last = None

    def _consume_last(self):
        if self._last is not None:
            try:
                self._last.fetchall()
            except Exception:
                pass
            self._last = None

    def execute(self, sql, params=None):
        if params is None:
            params = []
        self._consume_last()
        raw_cursor = self._conn.execute(sql, params)
        wrapper = CursorWrapper(raw_cursor, self)
        self._last = raw_cursor
        return wrapper

    def commit(self):
        self._consume_last()
        self._conn.commit()

    def rollback(self):
        self._consume_last()
        self._conn.rollback()

    def close(self):
        self._consume_last()
        self._conn.close()


class CursorWrapper:
    def __init__(self, cursor, db_wrapper=None):
        self._cursor = cursor
        self._db = db_wrapper

    def _done(self):
        if self._db and self._db._last is self._cursor:
            self._db._last = None

    def fetchone(self):
        try:
            row = self._cursor.fetchone()
            return _row_to_dict(row, self._cursor.description)
        finally:
            try:
                self._cursor.fetchall()
            except Exception:
                pass
            self._done()

    def fetchall(self):
        try:
            rows = self._cursor.fetchall()
            return _rows_to_list(rows, self._cursor.description)
        finally:
            self._done()

    def fetchval(self):
        try:
            row = self._cursor.fetchone()
            if row is None:
                return None
            val = row[0]
            try:
                self._cursor.fetchall()
            except Exception:
                pass
            return val
        finally:
            self._done()

    def __iter__(self):
        desc = self._cursor.description
        try:
            for row in self._cursor:
                yield _row_to_dict(row, desc)
        finally:
            self._done()

    def __getitem__(self, idx):
        rows = self.fetchall()
        return rows[idx]


def get_db():
    if 'db' not in g:
        db_path = current_app.config['DATABASE_PATH']
        raw_conn = duckdb.connect(db_path)
        raw_conn.execute("SET timezone = 'Asia/Shanghai'")
        g.db = DuckDBWrapper(raw_conn)
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db(app):
    with app.app_context():
        db = get_db()

        db.execute("""
            CREATE SEQUENCE IF NOT EXISTS seq_users START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_batches START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_question_groups START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_papers START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_scoring_rules START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_responsibility_groups START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_tasks START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_reviews START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_alerts START 1;
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_users'),
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) NOT NULL,
                real_name VARCHAR(100),
                group_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT true,
                CHECK (role IN ('admin', 'reviewer', 'auditor'))
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_batches'),
                batch_code VARCHAR(50) UNIQUE NOT NULL,
                batch_name VARCHAR(200) NOT NULL,
                description TEXT,
                status VARCHAR(20) DEFAULT 'active',
                start_date DATE,
                end_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER REFERENCES users(id),
                CHECK (status IN ('active', 'completed', 'suspended'))
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS question_groups (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_question_groups'),
                group_code VARCHAR(50) UNIQUE NOT NULL,
                group_name VARCHAR(200) NOT NULL,
                description TEXT,
                batch_id INTEGER,
                max_score DECIMAL(10,2) DEFAULT 100,
                pass_score DECIMAL(10,2) DEFAULT 60,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS papers (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_papers'),
                paper_number VARCHAR(50) UNIQUE NOT NULL,
                batch_id INTEGER REFERENCES batches(id),
                question_group_id INTEGER REFERENCES question_groups(id),
                candidate_name VARCHAR(200),
                candidate_id VARCHAR(100),
                paper_content TEXT,
                storage_path VARCHAR(500),
                current_status VARCHAR(30) DEFAULT 'pending_assignment',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (current_status IN (
                    'pending_assignment', 'reviewing', 'pending_audit',
                    'diff_pending', 'finalized', 'suspended'
                ))
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS scoring_rules (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_scoring_rules'),
                rule_code VARCHAR(50) UNIQUE NOT NULL,
                rule_name VARCHAR(200) NOT NULL,
                question_group_id INTEGER REFERENCES question_groups(id),
                description TEXT,
                criteria_json TEXT,
                score_guide TEXT,
                deduction_rules TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS responsibility_groups (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_responsibility_groups'),
                group_code VARCHAR(50) UNIQUE NOT NULL,
                group_name VARCHAR(200) NOT NULL,
                description TEXT,
                batch_id INTEGER,
                question_group_id INTEGER,
                deadline_hours INTEGER DEFAULT 48,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_tasks'),
                task_code VARCHAR(50) UNIQUE NOT NULL,
                paper_id INTEGER NOT NULL,
                task_type VARCHAR(20) NOT NULL,
                assignee_id INTEGER,
                group_id INTEGER,
                status VARCHAR(30) DEFAULT 'pending_assignment',
                assigned_at TIMESTAMP,
                started_at TIMESTAMP,
                deadline_at TIMESTAMP,
                completed_at TIMESTAMP,
                is_active BOOLEAN DEFAULT true,
                return_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (task_type IN ('review', 'audit')),
                CHECK (status IN (
                    'pending_assignment', 'reviewing', 'pending_audit',
                    'diff_pending', 'finalized', 'suspended', 'returned'
                ))
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_reviews'),
                task_id INTEGER NOT NULL,
                paper_id INTEGER NOT NULL,
                reviewer_id INTEGER,
                review_type VARCHAR(20) NOT NULL,
                initial_score DECIMAL(10,2),
                audit_score DECIMAL(10,2),
                final_score DECIMAL(10,2),
                deduction_reason TEXT,
                difficulty_flag BOOLEAN DEFAULT false,
                difficulty_note TEXT,
                completion_note TEXT,
                diff_reason TEXT,
                handling_opinion TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (review_type IN ('initial', 'audit'))
            );
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_alerts'),
                alert_type VARCHAR(50) NOT NULL,
                alert_level VARCHAR(20) DEFAULT 'warning',
                paper_id INTEGER,
                task_id INTEGER,
                question_group_id INTEGER,
                group_id INTEGER,
                message TEXT,
                detail_json TEXT,
                is_handled BOOLEAN DEFAULT false,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                handled_at TIMESTAMP,
                CHECK (alert_type IN (
                    'score_diff', 'timeout', 'difficulty_cluster',
                    'unfinalized_after_audit', 'backlog'
                )),
                CHECK (alert_level IN ('info', 'warning', 'critical'))
            );
        """)
        # TODO: DuckDB 0.10.0 索引bug，暂时禁用所有索引
        # db.execute("""
        #     CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(current_status);
        #     CREATE INDEX IF NOT EXISTS idx_papers_batch ON papers(batch_id);
        #     CREATE INDEX IF NOT EXISTS idx_papers_qg ON papers(question_group_id);
        #     CREATE INDEX IF NOT EXISTS idx_tasks_paper ON tasks(paper_id);
        #     CREATE INDEX IF NOT EXISTS idx_tasks_active ON tasks(paper_id, task_type, is_active);
        #     CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id, status);
        #     CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        #     CREATE INDEX IF NOT EXISTS idx_reviews_paper ON reviews(paper_id);
        #     CREATE INDEX IF NOT EXISTS idx_reviews_task ON reviews(task_id);
        #     CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type);
        #     CREATE INDEX IF NOT EXISTS idx_alerts_unhandled ON alerts(is_handled);
        # """)

        try:
            from werkzeug.security import generate_password_hash
            admin_hash = generate_password_hash('admin123')
            reviewer_hash = generate_password_hash('reviewer123')
            auditor_hash = generate_password_hash('auditor123')

            db.execute("""
                INSERT INTO users (username, password_hash, role, real_name) VALUES
                ('admin', ?, 'admin', '系统管理员'),
                ('reviewer1', ?, 'reviewer', '阅卷员甲'),
                ('reviewer2', ?, 'reviewer', '阅卷员乙'),
                ('auditor1', ?, 'auditor', '复核员甲')
                ON CONFLICT (username) DO NOTHING
            """, [admin_hash, reviewer_hash, reviewer_hash, auditor_hash])
        except Exception:
            pass

        db.commit()
