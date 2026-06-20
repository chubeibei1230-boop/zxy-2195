import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'language-assessment-secret-key-2024')
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'language-assessment-secret-key-2024')
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)
    DATABASE_PATH = os.environ.get('DATABASE_PATH', os.path.join(BASE_DIR, 'assessment.db'))
    PORT = int(os.environ.get('PORT', 8163))

    SCORE_DIFF_THRESHOLD = 5.0
    REVIEW_TIMEOUT_HOURS = 48
    DIFFICULTY_CLUSTER_THRESHOLD = 3
    BACKLOG_THRESHOLD = 10
