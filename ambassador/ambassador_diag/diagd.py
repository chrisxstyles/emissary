#!python

# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License

from typing import Any, Dict, List, Optional

import datetime
import functools
import json
import logging
import multiprocessing
import os
import time
import uuid

from pkg_resources import Requirement, resource_filename

import clize
from clize import Parameter
from flask import Flask, render_template, send_from_directory, request, jsonify
import gunicorn.app.base
from gunicorn.six import iteritems

from ambassador import Config, IR, EnvoyConfig, Diagnostics, Scout, Version
from ambassador.utils import SystemInfo, PeriodicTrigger, SplitConfigChecker

from ambassador.diagnostics import EnvoyStats

__version__ = Version

boot_time = datetime.datetime.now()

logging.basicConfig(
    level=logging.INFO,
    format="%%(asctime)s diagd %s [P%%(process)dT%%(threadName)s] %%(levelname)s: %%(message)s" % __version__,
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Shut up Werkzeug's standard request logs -- they're just too noisy.
logging.getLogger("werkzeug").setLevel(logging.CRITICAL)

# Likewise make requests a bit quieter.
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

ambassador_targets = {
    'mapping': 'https://www.getambassador.io/reference/configuration#mappings',
    'module': 'https://www.getambassador.io/reference/configuration#modules',
}

envoy_targets = {
    'route': 'https://envoyproxy.github.io/envoy/configuration/http_conn_man/route_config/route.html',
    'cluster': 'https://envoyproxy.github.io/envoy/configuration/cluster_manager/cluster.html',
}


def number_of_workers():
    return (multiprocessing.cpu_count() * 2) + 1


class DiagApp (Flask):
    estats: EnvoyStats
    config_dir_prefix: str
    health_checks: bool
    debugging: bool
    verbose: bool
    k8s: bool
    notice_path: str
    logger: logging.Logger
    scc: SplitConfigChecker
    aconf: Config
    notices: 'Notices'
    scout: Scout
    scout_args: Dict[str, Any]
    scout_result: Dict[str, Any]

# Get the Flask app defined early.
app = DiagApp(__name__,
              template_folder=resource_filename(Requirement.parse("ambassador"), "templates"))


######## DECORATORS

def standard_handler(f):
    func_name = getattr(f, '__name__', '<anonymous>')

    @functools.wraps(f)
    def wrapper(*args, **kwds):
        reqid = str(uuid.uuid4()).upper()
        prefix = "%s: %s \"%s %s\"" % (reqid, request.remote_addr, request.method, request.path)

        app.logger.info("%s START" % prefix)

        start = datetime.datetime.now()

        app.logger.debug("%s handler %s" % (prefix, func_name))

        # Default to the exception case
        result_to_log = "server error"
        status_to_log = 500
        result_log_level = logging.ERROR
        result = (result_to_log, status_to_log)

        try:
            result = f(*args, reqid=reqid, **kwds)
            if not isinstance(result, tuple):
                result = (result, 200)

            status_to_log = result[1]

            if (status_to_log // 100) == 2:
                result_log_level = logging.INFO
                result_to_log = "success"
            else:
                result_log_level = logging.ERROR
                result_to_log = "failure"
        except Exception as e:
            app.logger.exception(e)

        end = datetime.datetime.now()
        ms = int(((end - start).total_seconds() * 1000) + .5)

        app.logger.log(result_log_level, "%s %dms %d %s" % (prefix, ms, status_to_log, result_to_log))

        return result

    return wrapper


######## UTILITIES


class Notices:
    def __init__(self, local_config_path: str) -> None:
        self.local_path = local_config_path
        self.notices: List[Dict[str, str]] = []

    def reset(self):
        local_notices: List[Dict[str, str]] = []
        local_data = ''

        try:
            local_stream = open(self.local_path, "r")
            local_data = local_stream.read()
            local_notices = json.loads(local_data)
        except OSError:
            pass
        except:
            local_notices.append({ 'level': 'ERROR', 'message': 'bad local notices: %s' % local_data })

        self.notices = local_notices

    def post(self, notice):
        self.notices.append(notice)

    def prepend(self, notice):
        self.notices.insert(0, notice)

    def extend(self, notices):
        for notice in notices:
            self.post(notice)


def get_aconf(app) -> Config:
    # We need to find the sync-# directory with the highest number...
    sync_dirs = []

    for subdir in os.listdir(app.config_dir_prefix):
        if subdir.startswith("sync-"):
            try:
                sync_dirs.append(int(subdir.replace("sync-", "")))
            except ValueError:
                pass

    latest_generation = sorted(sync_dirs, reverse=True)[0]

    latest = os.path.join(app.config_dir_prefix, "sync-%d" % latest_generation)

    app.logger.debug("Fetching resources from %s" % latest)

    app.scc = SplitConfigChecker(app.logger, latest)

    aconf = Config()
    aconf.load_from_directory(latest, k8s=app.k8s, recurse=True)

    app.notices = Notices(app.notice_path)
    app.notices.reset()

    return aconf


def check_scout(app, what: str, ir: Optional[IR]=None) -> None:
    uptime = datetime.datetime.now() - boot_time
    hr_uptime = td_format(uptime)

    app.scout = Scout()
    app.scout_args = {
        "uptime": int(uptime.total_seconds()),
        "hr_uptime": hr_uptime
    }

    if ir and not os.environ.get("AMBASSADOR_DISABLE_FEATURES", None):
        app.scout_args["features"] = ir.features()

    app.scout_result = app.scout.report(mode="diagd", action=what, **app.scout_args)
    app.notices.extend(app.scout_result.pop('notices', []))

    app.logger.info("Scout reports %s" % json.dumps(app.scout_result))
    app.logger.info("Scout notices: %s" % json.dumps(app.notices.notices))


def td_format(td_object):
    seconds = int(td_object.total_seconds())
    periods = [
        ('year',   60*60*24*365),
        ('month',  60*60*24*30),
        ('day',    60*60*24),
        ('hour',   60*60),
        ('minute', 60),
        ('second', 1)
    ]

    strings = []
    for period_name, period_seconds in periods:
        if seconds > period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)

            strings.append("%d %s%s" % 
                           (period_value, period_name, "" if (period_value == 1) else "s"))

    formatted = ", ".join(strings)

    if not formatted:
        formatted = "0s"

    return formatted


def interval_format(seconds, normal_format, now_message):
    if seconds >= 1:
        return normal_format % td_format(datetime.timedelta(seconds=seconds))
    else:
        return now_message


def system_info():
    return {
        "version": __version__,
        "hostname": SystemInfo.MyHostName,
        "cluster_id": os.environ.get('AMBASSADOR_CLUSTER_ID',
                                     os.environ.get('AMBASSADOR_SCOUT_ID', "00000000-0000-0000-0000-000000000000")),
        "boot_time": boot_time,
        "hr_uptime": td_format(datetime.datetime.now() - boot_time)
    }


def envoy_status(estats):
    since_boot = interval_format(estats.time_since_boot(), "%s", "less than a second")

    since_update = "Never updated"

    if estats.time_since_update():
        since_update = interval_format(estats.time_since_update(), "%s ago", "within the last second")

    return {
        "alive": estats.is_alive(),
        "ready": estats.is_ready(),
        "uptime": since_boot,
        "since_update": since_update
    }


@app.route('/ambassador/v0/favicon.ico', methods=[ 'GET' ])
def favicon():
    template_path = resource_filename(Requirement.parse("ambassador"), "templates")

    return send_from_directory(template_path, "favicon.ico")


@app.route('/ambassador/v0/check_alive', methods=[ 'GET' ])
def check_alive():
    status = envoy_status(app.estats)

    if status['alive']:
        return "ambassador liveness check OK (%s)" % status['uptime'], 200
    else:
        return "ambassador seems to have died (%s)" % status['uptime'], 503


@app.route('/ambassador/v0/check_ready', methods=[ 'GET' ])
def check_ready():
    status = envoy_status(app.estats)

    if status['ready']:
        return "ambassador readiness check OK (%s)" % status['since_update'], 200
    else:
        return "ambassador not ready (%s)" % status['since_update'], 503


@app.route('/ambassador/v0/diag/', methods=[ 'GET' ])
@standard_handler
def show_overview(reqid=None):
    app.logger.debug("OV %s - showing overview" % reqid)

    aconf = get_aconf(app)
    ir = IR(aconf, secret_reader=app.scc.secret_reader)
    check_scout(app, "overview", ir)

    econf = EnvoyConfig.generate(ir, "V2")
    diag = Diagnostics(ir, econf)

    if app.verbose:
        app.logger.debug("OV %s: DIAG" % reqid)
        app.logger.debug("%s" % json.dumps(diag.as_dict(), sort_keys=True, indent=4))

    ov = diag.overview(request, app.estats)

    if app.verbose:
        app.logger.debug("OV %s: OV" % reqid)
        app.logger.debug("%s" % json.dumps(ov, sort_keys=True, indent=4))
        app.logger.debug("OV %s: collecting errors" % reqid)

    ddict = collect_errors_and_notices(request, reqid, "overview", diag)

    tvars = dict(system=system_info(),
                 envoy_status=envoy_status(app.estats), 
                 loginfo=app.estats.loginfo,
                 notices=app.notices.notices,
                 **ov, **ddict)

    if request.args.get('json', None):
        key = request.args.get('filter', None)

        if key:
            return jsonify(tvars.get(key, None))
        else:
            return jsonify(tvars)
    else:
        return render_template("overview.html", **tvars)


def collect_errors_and_notices(request, reqid, what: str, diag: Diagnostics) -> Dict:
    loglevel = request.args.get('loglevel', None)
    notice = None

    if loglevel:
        app.logger.debug("%s %s -- requesting loglevel %s" % (what, reqid, loglevel))

        if not app.estats.update_log_levels(time.time(), level=loglevel):
            notice = { 'level': 'WARNING', 'message': "Could not update log level!" }
        # else:
        #     return redirect("/ambassador/v0/diag/", code=302)

    # We need to grab errors and notices from diag.as_dict(), process the errors so
    # they work for the HTML rendering, and post the notices to app.notices. Then we
    # return the dict representation that our caller should work with.

    ddict = diag.as_dict()

    # app.logger.debug("ddict %s" % json.dumps(ddict, indent=4, sort_keys=True))

    derrors = ddict.pop('errors', {})

    errors = []

    for err_key, err_list in derrors.items():
        if err_key == "-global-":
            err_key = ""

        for err in err_list:
            errors.append((err_key, err[ 'error' ]))

    dnotices = ddict.pop('notices', {})

    # Make sure that anything about the loglevel gets folded into this set.
    if notice:
        app.notices.prepend(notice)

    for notice_key, notice_list in dnotices.items():
        for notice in notice_list:
            app.logger.debug("POSTING NOTICE %s %s" % (notice_key, notice))
            app.notices.post({'level': 'NOTICE', 'message': "%s: %s" % (notice_key, notice)})

    ddict['errors'] = errors

    return ddict


@app.route('/ambassador/v0/diag/<path:source>', methods=[ 'GET' ])
@standard_handler
def show_intermediate(source=None, reqid=None):
    app.logger.debug("SRC %s - getting intermediate for '%s'" % (reqid, source))

    aconf = get_aconf(app)
    ir = IR(aconf, secret_reader=app.scc.secret_reader)
    check_scout(app, "detail: %s" % source, ir)

    econf = EnvoyConfig.generate(ir, "V2")
    diag = Diagnostics(ir, econf)

    method = request.args.get('method', None)
    resource = request.args.get('resource', None)

    result = diag.lookup(request, source, app.estats)

    if app.verbose:
        app.logger.debug("RESULT %s" % json.dumps(result, sort_keys=True, indent=4))

    ddict = collect_errors_and_notices(request, reqid, "detail %s" % source, diag)

    tvars = dict(system=system_info(),
                 envoy_status=envoy_status(app.estats),
                 loginfo=app.estats.loginfo,
                 notices=app.notices.notices,
                 method=method, resource=resource,
                 **result, **ddict)

    if request.args.get('json', None):
        key = request.args.get('filter', None)

        if key:
            return jsonify(tvars.get(key, None))
        else:
            return jsonify(tvars)
    else:
        return render_template("diag.html", **tvars)


@app.template_filter('sort_by_key')
def sort_by_key(objects):
    return sorted(objects, key=lambda x: x['key'])


@app.template_filter('pretty_json')
def pretty_json(obj):
    if isinstance(obj, dict):
        obj = dict(**obj)

        keys_to_drop = [ key for key in obj.keys() if key.startswith('_') ]

        for key in keys_to_drop:
            del(obj[key])

    return json.dumps(obj, indent=4, sort_keys=True)


@app.template_filter('sort_clusters_by_service')
def sort_clusters_by_service(clusters):
    return sorted(clusters, key=lambda x: x['service'])
    # return sorted([ c for c in clusters.values() ], key=lambda x: x['service'])


@app.template_filter('source_lookup')
def source_lookup(name, sources):
    app.logger.info("%s => sources %s" % (name, sources))

    source = sources.get(name, {})

    app.logger.info("%s => source %s" % (name, source))

    return source.get('_source', name)


def create_diag_app(config_dir_path, do_checks=False, reload=False, debug=False, k8s=True, verbose=False, notices=None):
    app.estats = EnvoyStats()
    app.health_checks = False
    app.debugging = reload
    app.verbose = verbose
    app.k8s = k8s
    app.notice_path = notices

    # This feels like overkill.
    app.logger = logging.getLogger("ambassador.diagd")
    app.logger.setLevel(logging.INFO)

    if debug:
        app.logger.setLevel(logging.DEBUG)
        logging.getLogger('ambassador').setLevel(logging.DEBUG)

    if do_checks:
        app.health_checks = True

    app.config_dir_prefix = config_dir_path

    return app


class StandaloneApplication(gunicorn.app.base.BaseApplication):
    def __init__(self, app, options=None):
        self.options = options or {}
        self.application = app
        super(StandaloneApplication, self).__init__()

    def load_config(self):
        config = dict([(key, value) for key, value in iteritems(self.options)
                       if key in self.cfg.settings and value is not None])
        for key, value in iteritems(config):
            self.cfg.set(key.lower(), value)

    def load(self):
        if self.application.health_checks:
            self.application.logger.info("Starting periodic updates")
            self.application.stats_updater = PeriodicTrigger(self.application.estats.update, period=5)

        return self.application


def _main(config_dir_path: Parameter.REQUIRED, *, no_checks=False, reload=False, debug=False, verbose=False,
          workers=None, port=8877, host='0.0.0.0', k8s=False, notices=None):
    """
    Run the diagnostic daemon.

    :param config_dir_path: Configuration directory to scan for Ambassador YAML files
    :param no_checks: If True, don't do Envoy-cluster health checking
    :param reload: If True, run Flask in debug mode for live reloading
    :param debug: If True, do debug logging
    :param verbose: If True, do really verbose debug logging
    :param workers: Number of workers; default is based on the number of CPUs present
    :param host: Interface on which to listen (default 0.0.0.0)
    :param port: Port on which to listen (default 8877)
    :param notices: Optional file to read for local notices
    """
    
    # Create the application itself.
    flask_app = create_diag_app(config_dir_path, not no_checks, reload, debug, k8s, verbose, notices)

    if not workers:
        workers = number_of_workers()

    gunicorn_config = {
        'bind': '%s:%s' % (host, port),
        # 'workers': 1,
        'threads': workers,
    }

    app.logger.info("thread count %d, listening on %s" % (gunicorn_config['threads'], gunicorn_config['bind']))

    StandaloneApplication(flask_app, gunicorn_config).run()


def main():
    clize.run(_main)


if __name__ == "__main__":
    main()
