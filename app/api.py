from flask import request, g
from flask.json import jsonify
from flask_restful import Api, Resource, reqparse
from flask_httpauth import HTTPBasicAuth
from requests import get
from apscheduler.jobstores.base import ConflictingIdError
from sqlalchemy.exc import IntegrityError
from .models import User, ScanJob, LinkCheck, ScheduledJob, PermissionedURL
from . import app, scheduler, db
from .link_check import LinkChecker, scheduled_scan, async_scan, standardize_url

# auth setup
auth = HTTPBasicAuth()
@auth.verify_password
def verify_password(username, password):
    user = User.query.filter_by(username=username).first()
    if not user or not user.verify_password(password):
        return False
    g.user = user
    return True


class Resource(Resource):
    """Adds decorates for all classes inheriting from resource"""
    method_decorators = [auth.login_required]


class HistoricalResults(Resource):
    def get(self):
        """Return the results of a historical job for a given user.
        Optionally, specify a root URL, job ID, and/or owner to filter results
        """
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner', type=str, help='Scan job owner')
        parser.add_argument('job_id', type=str, help='Job ID')
        args = parser.parse_args()
        if (not args.owner) or (not g.user.admin):
            owner = g.user.username
        else:
            owner = args.owner
        if args.job_id:
            job_id = args.job_id
            last_job_results = LinkCheck.query.filter(LinkCheck.job_id == args.job_id).all()
        else:
            last_job_id = db.session.query(db.func.max(ScanJob.id)).\
                filter(ScanJob.user == g.user).\
                filter(ScanJob.owner == owner)
            if args.url:
                last_job_id = last_job_id.filter(ScanJob.root_url == standardize_url(args.url))
            try:
                last_job = ScanJob.query.filter(ScanJob.id == last_job_id.scalar()).all()[-1]
            except IndexError:
                return jsonify([])
            last_job_results = LinkCheck.query.filter(LinkCheck.job == last_job).all()
        return jsonify(
            job_status=last_job.status,
            results=[result.to_json() for result in last_job_results])


class HistoricalJobs(Resource):
    def get(self):
        """List all historical jobs for a given user.
        Optionally, specify a root URL and/or owner to filter results
        """
        parser = reqparse.RequestParser()
        parser.add_argument('url', type=str, help='URL to check')
        parser.add_argument('owner', type=str, help='Scan job owner')
        args = parser.parse_args()
        if (not args.owner) or (not g.user.admin):
            owner = g.user.username
        else:
            owner = args.owner
        jobs = ScanJob.query.\
            filter(ScanJob.user == g.user).\
            filter(ScanJob.owner == owner)
        if args.url:
            jobs = jobs.filter(ScanJob.root_url == standardize_url(args.url))
        return jsonify([job.to_json() for job in jobs])


class LinkScan(Resource):
    def post(self):
        """Scan a website for 404 errors"""
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner', type=str, help='Scan job owner')
        args = parser.parse_args()
        if (not args.owner) or (not g.user.admin):
            owner = g.user.username
        else:
            owner = args.owner
        # confirm url is permissioned for owner-user
        permissioned = PermissionedURL.query.\
            filter(PermissionedURL.root_url == standardize_url(args.url)).\
            filter(PermissionedURL.owner == owner).\
            filter(PermissionedURL.user == g.user).count()
        if permissioned == 0:
            response = jsonify(
                message='User-owner is not permissioned for this website')
            response.status_code = 403
            return response
        job, _ = async_scan(args.url, g.user, owner)
        return jsonify(job.to_json())

class LinkScanJob(Resource):
    def post(self):
        """Post a recurring scan job"""
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner', type=str, help='Scan job owner')
        args = parser.parse_args()
        if (not args.owner) or (not g.user.admin):
            owner = g.user.username
        else:
            owner = args.owner
        cron_params = request.get_json()
        # confirm url is permissioned for owner-user
        permissioned = PermissionedURL.query.\
            filter(PermissionedURL.root_url == (args.url)).\
            filter(PermissionedURL.owner == owner).\
            filter(PermissionedURL.user == g.user).count()
        if permissioned == 0:
            response = jsonify(
                message='User-owner is not permissioned for this website')
            response.status_code = 403
            return response
        try:
            job = scheduled_scan(args.url, g.user, cron_params, owner)
            return jsonify(job_id=job.id)
        except ConflictingIdError:
            response = jsonify(message='This user already has a scheduled job for the root URL provided.')
            response.status_code = 403
            return response

    def get(self):
        """List scheduled jobs for a given user.
        Optionally, specify a root URL and/or owner to filter results
        """
        parser = reqparse.RequestParser()
        parser.add_argument('url', type=str, help='URL to check')
        parser.add_argument('owner', type=str, help='Scan job owner')
        args = parser.parse_args()
        if (not args.owner) or (not g.user.admin):
            owner = g.user.username
        else:
            owner = args.owner
        jobs = ScheduledJob.query.\
            filter(ScheduledJob.user == g.user).\
            filter(ScheduledJob.owner == owner)
        if args.url:
            jobs = jobs.filter(ScheduledJob.root_url == standardize_url(args.url))
        scheduled_jobs = filter(
            lambda x: x is not None,
            [scheduler.get_job(str(job.id)) for job in jobs])
        return jsonify([
            dict(next_run_time=job.next_run_time, trigger=str(job.trigger))
            for job in scheduled_jobs
        ])


class UrlPermissions(Resource):
    def post(self):
        """Add permissioned URL for a given user (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner', type=str, help='Scan job owner')
        args = parser.parse_args()
        if (not args.owner) or (not g.user.admin):
            owner = g.user.username
        else:
            owner = args.owner
        if not g.user.admin:
            response = jsonify(message='This method requires admin rights.')
            response.status_code = 403
            return response
        try:
            permissioned_url = PermissionedURL(
                root_url=standardize_url(args.url),
                user=g.user,
                owner=owner,
            )
            db.session.add(permissioned_url)
            db.session.commit()
            return jsonify(permissioned_url.to_json())
        except IntegrityError:
            response = jsonify(
                message='URl already permissioned')
            response.status_code = 403
            return response

    def get(self):
        """Add permissioned URL for a given user (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('owner', type=str, help='Scan job owner')
        args = parser.parse_args()
        if (not args.owner) or (not g.user.admin):
            owner = g.user.username
        else:
            owner = args.owner
        if not g.user.admin:
            response = jsonify(message='This method requires admin rights.')
            response.status_code = 403
            return response
        permissioned_urls = PermissionedURL.query.\
            filter(PermissionedURL.owner == owner).\
            filter(PermissionedURL.user == g.user)
        return jsonify([
            permissioned_url.root_url
            for permissioned_url in permissioned_urls])


api = Api(app)
api.add_resource(LinkScan, "/link-scan")
api.add_resource(HistoricalJobs, "/jobs/historical")
api.add_resource(HistoricalResults, "/results/historical")
api.add_resource(LinkScanJob, "/link-scan/schedule")
api.add_resource(UrlPermissions, "/permissions")
