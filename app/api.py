from flask import request, g
from flask.json import jsonify
from flask_restful import Api, Resource, reqparse, inputs
from requests import get
import datetime
from apscheduler.jobstores.base import ConflictingIdError
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from .models import User, ScanJob, LinkCheck, ScheduledJob, PermissionedURL, Owner, Link, Exception
from . import app, scheduler, db
from .link_check import LinkChecker, standardize_descheme_url
from .email import send_email
from .auth import auth


def email_results(job):
    job_results = LinkCheck.query.filter(LinkCheck.job == job).\
        outerjoin(Exception, Exception.exception == LinkCheck.exception).\
        with_entities(
            LinkCheck,  # For severity assessment
            Exception,
        )

    # get sources
    link_sources = Link.query.\
        filter(Link.url.in_(job_results.with_entities(LinkCheck.url))).\
        filter(Link.job == job).\
        with_entities(Link.url, Link.source_url).all()

    # generate email message body
    errors = [result for result in job_results.all() if result.LinkCheck.severity > 0]
    n_errors = len(errors)
    message = "<p>Hi,</p><p>I just finished scanning {}, and I found {} potential error{} I think you should review{}</p>".format(
        '<a href="{}">{}</a>'.format(job.root_url, job.root_url),
        n_errors,
        's' if n_errors != 1 else '',
        ':' if n_errors > 0 else '.',
    )

    if n_errors > 0:
        message += '<ul>'
        for error in errors:
            if error.LinkCheck.response:
                error_description = str(error.LinkCheck.response) + ' response'
            else:
                error_description = error.Exception.exception_description
                if error_description.endswith('.'):
                    error_description = error_description[:-1]
            message += '<li>{} [{}]</li>'.format(
                error.LinkCheck.url, error_description)
        message += '</ul>'
    message += '<p>The full results for this scan can be viewed at <a href="https://404sentry.com/dashboard">404sentry.com</a>.</p><p>As always, please don\'t hesitate to respond to this email with any questions, comments or concerns.</p><p>Thanks,<br>404 Sentry</p>'

    send_email(
        to_address=job.owner.stripe_email,
        to_name="",  #TODO dynamically retreive name
        subject="Your 404 Sentry scan results for {}".format(job.root_url),
        message_content=message,
    )

    return dict(
        to_address=job.owner.stripe_email,
        to_name="",
        subject="Your 404 Sentry scan results for {}".format(job.root_url),
        message_content=message,
    )

def scan(*args, **kwargs):
    with app.app_context():
        print('Scanning [{}]'.format(datetime.datetime.now().time()))
        email = kwargs.pop('email', False)

        owner_id = kwargs.pop('owner_id')
        owner = Owner.query.filter(Owner.id == owner_id).first()
        kwargs['owner'] = owner

        user_id = kwargs.pop('user_id')
        user = User.query.filter(User.id == user_id).first()
        kwargs['user'] = user

        checker = LinkChecker(*args, **kwargs)
        checker.check_all_links_and_follow()
        checker.report_errors(lambda status: status == 404)
        checker.job.status='completed'
        db.session.commit()
        if email:
            print('Sending email')
            email_results(checker.job)


def async_scan(url, user, owner=None):
    scan_record = ScheduledJob(root_url=url, owner=owner, user=user)
    db.session.add(scan_record)
    db.session.commit()
    job_params_base = {
        'id': str(scan_record.id),
        'func': scan,
        'kwargs': dict(
            url=url,
            user_id=str(user.id),
            owner_id=str(owner.id),
        ),
        'trigger': 'date',
    }
    job_params = {**job_params_base}
    return scan_record, scheduler.add_job(**job_params)


def scheduled_scan(url, user, cron_params, owner=None):
    scan_record = ScheduledJob(root_url=url, owner=owner, user=user)
    db.session.add(scan_record)
    db.session.commit()
    job_params_base = {
        'id': str(scan_record.id),
        'func': scan,
        'kwargs': dict(
            url=url,
            user_id=str(user.id),
            owner_id=str(owner.id),
            email=True,
        ),
        'trigger': 'cron',
    }
    job_params = {**job_params_base, **cron_params}
    return scheduler.add_job(**job_params)


class Resource(Resource):
    """Adds decorates for all classes inheriting from resource"""
    method_decorators = [auth.login_required]


def get_owner_id(owner_id):
    """ Sets `owner_id` to API username if owner is not an admin or now `owner_id` provided.
    """
    if (not owner_id) or (not g.user.admin):
        return g.user.username
    else:
        return owner_id

def get_owner(owner_id):
    """ Get Owner record from `owner_id`.
    Sets `owner_id` to API username if owner is not an admin or now `owner_id` provided.
    """
    owner_id = get_owner_id(owner_id)
    return Owner.query.filter(Owner.user == g.user).filter(Owner.email == owner_id).first()


def get_last_job(owner, url):
    """ Get most recent ScanJob record for the corresponding filters
    """
    last_job_id = db.session.query(db.func.max(ScanJob.id)).\
        filter(ScanJob.user == g.user).\
        filter(ScanJob.owner == owner)
    if url:
        last_job_id = last_job_id.filter(ScanJob.root_url == standardize_descheme_url(url))
    return ScanJob.query.filter(ScanJob.id == last_job_id.scalar()).all()[-1]


def admin_required(f):
    """ Decorator to confirm the current API user is an admin; returns a 403 otherwise.
    """
    def wrapper(*args, **kwargs):
        if not g.user.admin:
            response = jsonify(message='This method requires admin rights.')
            response.status_code = 403
            return response
        return f(*args, **kwargs)
    return wrapper

class HistoricalResults(Resource):
    def get(self):
        """Return the results of a historical job for a given user.
        Optionally, specify a root URL and/or owner to filter results
        """
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner ID')
        parser.add_argument('limit', type=int, default=100, help='Number of records to fetch')
        parser.add_argument('offset', type=int, default=0, help='First record to fetch')
        parser.add_argument('filter_exceptions', type=inputs.boolean, default=True, help='First exceptions only')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        try:
            last_job = get_last_job(owner, args.url)
        except IndexError:
            response = jsonify(message='Job not found')
            response.status_code = 404
            return response
        last_job_results = LinkCheck.query.\
            filter(LinkCheck.job == last_job)

        # filter exceptions
        if args.filter_exceptions:
            last_job_results = last_job_results.filter(LinkCheck.severity > 0).\
                order_by(LinkCheck.severity.desc())
        else:
            last_job_results = last_job_results.filter(LinkCheck.severity == 0)

        last_job_results = last_job_results.\
            outerjoin(Exception, Exception.exception == LinkCheck.exception).\
            order_by(LinkCheck.id)

        # limit results
        if args.limit:
            last_job_results = last_job_results.limit(args.limit)

        last_job_results = last_job_results.\
            offset(args.offset).\
            with_entities(
                LinkCheck,  # For severity assessment
                LinkCheck.id,
                LinkCheck.job_id,
                LinkCheck.note,
                LinkCheck.response,
                LinkCheck.url,
                Exception.exception_description,
            )

        # get sources
        link_sources = Link.query.\
            filter(Link.url.in_(last_job_results.with_entities(LinkCheck.url))).\
            filter(Link.job == last_job).\
            with_entities(Link.url, Link.source_url).distinct()

        # format sources > error mapping
        source_report = {}
        for link_source in link_sources.all():
            source_url = link_source.source_url
            url = link_source.url
            source_report[url] = source_report.get(url, []) + [source_url]

        # format results for consumption
        results = [
            dict(
                severity=result[0].severity,
                **{key: result[i + 1] for i, key in enumerate(result.keys()[1:])}
            )
            for result in last_job_results.all()]
        # override note with clean exception description
        for result in results:
            result['note'] = result.get('exception_description', result['note'])

        return jsonify(
            job=last_job.to_json(),
            results=results,
            sources=source_report,
        )


class HistoricalJobs(Resource):
    def get(self):
        """List all historical jobs for a given user.
        Optionally, specify a root URL and/or owner to filter results
        """
        parser = reqparse.RequestParser()
        parser.add_argument('url', type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        jobs = ScanJob.query.\
            filter(ScanJob.user == g.user).\
            filter(ScanJob.owner == owner)
        if args.url:
            jobs = jobs.filter(ScanJob.root_url == standardize_descheme_url(args.url))
        if jobs.count() == 0:
            response = jsonify(message='Job not found')
            response.status_code = 404
            return response
        return jsonify([
            job.to_json() for job in
            jobs.order_by(ScanJob.start_time.desc()).all()])


class LinkScan(Resource):
    def post(self):
        """Scan a website for 404 errors"""
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        # confirm url is permissioned for owner-user
        permissioned = PermissionedURL.query.\
            filter(PermissionedURL.root_url == standardize_descheme_url(args.url)).\
            filter(PermissionedURL.owner == owner).\
            filter(PermissionedURL.user == g.user).count()
        if permissioned == 0:
            response = jsonify(
                message='User-owner is not permissioned for this website')
            response.status_code = 403
            return response
        job, _ = async_scan(standardize_descheme_url(args.url), g.user, owner)
        return jsonify(job.to_json())

class LinkScanJob(Resource):
    def post(self):
        """Post a recurring scan job"""
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        cron_params = request.get_json()
        # confirm url is permissioned for owner-user
        permissioned = PermissionedURL.query.\
            filter(PermissionedURL.root_url == standardize_descheme_url(args.url)).\
            filter(PermissionedURL.owner == owner).\
            filter(PermissionedURL.user == g.user).count()
        if permissioned == 0:
            response = jsonify(
                message='User-owner is not permissioned for this website')
            response.status_code = 403
            return response
        try:
            job = scheduled_scan(standardize_descheme_url(args.url), g.user, cron_params, owner)
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
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        jobs = ScheduledJob.query.\
            filter(ScheduledJob.user == g.user).\
            filter(ScheduledJob.owner == owner)
        if args.url:
            jobs = jobs.filter(ScheduledJob.root_url == standardize_descheme_url(args.url))
        scheduled_jobs = filter(
            lambda x: x is not None,
            [scheduler.get_job(str(job.id)) for job in jobs])
        return jsonify([
            dict(
                next_run_time=job.next_run_time,
                trigger=str(job.trigger),
                cron_pattern=str(job.trigger)[:-1].split('[')[1],
            )
            for job in scheduled_jobs
        ])

    def delete(self):
        """List scheduled jobs for a given user.
        Optionally, specify a root URL and/or owner to filter results
        """
        parser = reqparse.RequestParser()
        parser.add_argument('url', type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        jobs = ScheduledJob.query.\
            filter(ScheduledJob.user == g.user).\
            filter(ScheduledJob.owner == owner)
        if args.url:
            jobs = jobs.filter(ScheduledJob.root_url == standardize_descheme_url(args.url))
        scheduled_jobs_summary = []
        for job in jobs:
            scheduled_job = scheduler.get_job(str(job.id))
            if scheduled_job is not None:
                scheduled_job.remove()
                scheduled_jobs_summary.append(job.to_json())
                db.session.delete(job)
        db.session.commit()
        return scheduled_jobs_summary


class UrlPermissions(Resource):

    @admin_required
    def post(self):
        """Add permissioned URL for a given user (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        parser.add_argument(
            'stripe_subscription_id', type=str, help='Stripe subscription ID')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        try:
            permissioned_url = PermissionedURL(
                root_url=standardize_descheme_url(args.url),
                user=g.user,
                owner=owner,
                stripe_subscription_id=args.stripe_subscription_id,
            )
            db.session.add(permissioned_url)
            db.session.commit()
            return jsonify(permissioned_url.to_json())
        except IntegrityError:
            db.session.rollback()
            permissioned_url = PermissionedURL.query.\
                filter(PermissionedURL.root_url == standardize_descheme_url(args.url)).\
                filter(PermissionedURL.user == g.user).\
                filter(PermissionedURL.owner == owner).first()
            response = jsonify(
                message='URl already permissioned',
                permissioned_url=permissioned_url.to_json(),
            )
            response.status_code = 403
            return response

    @admin_required
    def patch(self):
        """Update permissioned URL for a given user
        with provided URL paramters by job ID(requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('id', required=True, type=str, help='Permissioned URL ID')
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        parser.add_argument(
            'stripe_subscription_id', required=True, type=str, help='Stripe subscription ID')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        permissioned_url = PermissionedURL.query.filter(PermissionedURL.id == args.id).first()
        permissioned_url.stripe_subscription_id = args.stripe_subscription_id
        db.session.commit()
        return jsonify(permissioned_url.to_json())

    @admin_required
    def delete(self):
        """Delete permissioned URL for a given user (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('url', required=True, type=str, help='URL to check')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        depermissioned_urls = PermissionedURL.query.\
            filter(PermissionedURL.root_url == standardize_descheme_url(args.url)).\
            filter(PermissionedURL.user == g.user).\
            filter(PermissionedURL.owner == owner).all()
        if len(depermissioned_urls) == 0:
            response = jsonify(
                message='Resource not found')
            response.status_code = 404
            return response
        depermissioned_url_details = [url.to_json() for url in depermissioned_urls]
        for depermissioned_url in depermissioned_urls:
            db.session.delete(depermissioned_url)
        db.session.commit()
        return jsonify(depermissioned_url_details)

    @admin_required
    def get(self):
        """Add permissioned URL for a given user (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner = get_owner(args.owner_id)

        if not owner:
            response = jsonify(message='Owner not found')
            response.status_code = 404
            return response
        permissioned_urls = PermissionedURL.query.\
            filter(PermissionedURL.owner == owner).\
            filter(PermissionedURL.user == g.user)
        return jsonify([
            permissioned_url.to_json()
            for permissioned_url in permissioned_urls])

class Owners(Resource):

    @admin_required
    def post(self):
        """Add owner resource (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('stripe_token', type=str, help='Stripe token')
        parser.add_argument('stripe_email', type=str, help='Email from Stripe checkout')
        parser.add_argument('stripe_customer_id', type=str, help='Stripe customer ID')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        args = parser.parse_args()
        owner_id = get_owner_id(args.owner_id)

        try:
            owner = Owner(
                email=owner_id,
                user=g.user,
                stripe_token=args.stripe_token,
                stripe_email=args.stripe_email,
                stripe_customer_id=args.stripe_customer_id,
            )
            db.session.add(owner)
            db.session.commit()
            return jsonify(owner.to_json())
        except IntegrityError:
            db.session.rollback()
            owner = Owner.query.\
                filter(Owner.email == owner_id).\
                filter(Owner.user == g.user).first()
            response = jsonify(message='Owner already exists', owner=owner.to_json())
            response.status_code = 403
            return response

    @admin_required
    def patch(self):
        """Patch owner by owner ID (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('stripe_token', type=str, help='Stripe token')
        parser.add_argument('stripe_email', type=str, help='Email from Stripe checkout')
        parser.add_argument('stripe_customer_id', type=str, help='Stripe customer ID')
        parser.add_argument('owner_id', type=str, help='Scan job owner')
        parser.add_argument('id', required=True, type=int, help='API owner ID')
        args = parser.parse_args()
        owner_id = get_owner_id(args.owner_id)

        try:
            owner = Owner.query.filter(Owner.id == args.id).first()
            owner.email=owner_id
            owner.user=g.user
            owner.stripe_token=args.stripe_token
            owner.stripe_email=args.stripe_email
            owner.stripe_customer_id=args.stripe_customer_id
            db.session.commit()
            return jsonify(owner.to_json())
        except AttributeError:
            db.session.rollback()
            response = jsonify(message='Owner does not exist')
            response.status_code = 404
            return response

    @admin_required
    def get(self):
        """Get owner resource (requires admin rights)"""
        parser = reqparse.RequestParser()
        parser.add_argument('owner_id', required=True, type=str)
        args = parser.parse_args()
        owner_id = get_owner_id(args.owner_id)

        try:
            owner = Owner.query.\
                filter(Owner.email == owner_id).\
                filter(Owner.user == g.user).first()
            return jsonify(owner.to_json())
        except AttributeError:
            response = jsonify(message='Owner does not exist')
            response.status_code = 404
            return response


api = Api(app)
api.add_resource(LinkScan, "/link-scan")
api.add_resource(HistoricalJobs, "/jobs/historical")
api.add_resource(HistoricalResults, "/results/historical")
api.add_resource(LinkScanJob, "/link-scan/schedule")
api.add_resource(UrlPermissions, "/permissions")
api.add_resource(Owners, "/owners")
