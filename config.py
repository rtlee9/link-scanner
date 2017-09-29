import os
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
basedir = os.path.abspath(os.path.dirname(__file__))


class Config(object):
    DEBUG = True
    TESTING = True
    CSRF_ENABLED = True
    SQLALCHEMY_DATABASE_URI = os.getenv('POSTGRES_URL')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # JOBS = []
    SCHEDULER_JOBSTORES = {
        'default': SQLAlchemyJobStore(url=os.getenv('POSTGRES_URL'))
    }
    SCHEDULER_EXECUTORS = {
        'default': {'type': 'threadpool', 'max_workers': 20}
    }
    SCHEDULER_JOB_DEFAULTS = {
        'coalesce': False,
        'max_instances': 3
    }
    SCHEDULER_API_ENABLED = True


class ProductionConfig(Config):
    DEBUG = False


class StagingConfig(Config):
    DEVELOPMENT = True
    DEBUG = True


class DevelopmentConfig(Config):
    DEVELOPMENT = True
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
