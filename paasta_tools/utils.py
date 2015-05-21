from __future__ import print_function
from subprocess import PIPE
from subprocess import Popen
from subprocess import STDOUT
import contextlib
import datetime
import errno
import logging
import os
import pwd
import re
import shlex
import sys
import tempfile
import threading

import clog
import dateutil.tz
import docker
import json
import yaml


INFRA_ZK_PATH = '/nail/etc/zookeeper_discovery/infrastructure/'
PATH_TO_SYSTEM_PAASTA_CONFIG = '/etc/paasta_tools/paasta.json'
DEPLOY_PIPELINE_NON_DEPLOY_STEPS = (
    'itest',
    'security-check',
    'performance-check',
    'push-to-registry'
)
# Default values for _log
ANY_CLUSTER = 'N/A'
ANY_INSTANCE = 'N/A'
DEFAULT_LOGLEVEL = 'event'
no_escape = re.compile('\x1B\[[0-9;]*[mK]')


class PaastaColors:
    """Collection of static variables and methods to assist in coloring text."""
    # ANSI colour codes
    BLUE = '\033[34m'
    BOLD = '\033[1m'
    CYAN = '\033[36m'
    DEFAULT = '\033[0m'
    GREEN = '\033[32m'
    GREY = '\033[1m\033[30m'
    RED = '\033[31m'
    YELLOW = '\033[33m'

    @staticmethod
    def bold(text):
        """Return bolded text.

        :param text: a string
        :return: text colour coded with ANSI bold
        """
        return PaastaColors.color_text(PaastaColors.BOLD, text)

    @staticmethod
    def blue(text):
        """Return text that can be printed blue.

        :param text: a string
        :return: text colour coded with ANSI blue
        """
        return PaastaColors.color_text(PaastaColors.BLUE, text)

    @staticmethod
    def green(text):
        """Return text that can be printed green.

        :param text: a string
        :return: text colour coded with ANSI green"""
        return PaastaColors.color_text(PaastaColors.GREEN, text)

    @staticmethod
    def red(text):
        """Return text that can be printed red.

        :param text: a string
        :return: text colour coded with ANSI red"""
        return PaastaColors.color_text(PaastaColors.RED, text)

    @staticmethod
    def color_text(color, text):
        """Return text that can be printed color.

        :param color: ANSI colour code
        :param text: a string
        :return: a string with ANSI colour encoding"""
        # any time text returns to default, we want to insert our color.
        replaced = text.replace(PaastaColors.DEFAULT, PaastaColors.DEFAULT + color)
        # then wrap the beginning and end in our color/default.
        return color + replaced + PaastaColors.DEFAULT

    @staticmethod
    def cyan(text):
        """Return text that can be printed cyan.

        :param text: a string
        :return: text colour coded with ANSI cyan"""
        return PaastaColors.color_text(PaastaColors.CYAN, text)

    @staticmethod
    def yellow(text):
        """Return text that can be printed yellow.

        :param text: a string
        :return: text colour coded with ANSI yellow"""
        return PaastaColors.color_text(PaastaColors.YELLOW, text)

    @staticmethod
    def grey(text):
        return PaastaColors.color_text(PaastaColors.GREY, text)

    @staticmethod
    def default(text):
        return PaastaColors.color_text(PaastaColors.DEFAULT, text)


LOG_COMPONENTS = {
    'build': {
        'color': PaastaColors.blue,
        'help': 'Jenkins build jobs output, like the itest, promotion, security checks, etc.',
        'command': 'NA - TODO: tee jenkins build steps into scribe PAASTA-201',
        'source_env': 'env1',
    },
    'deploy': {
        'color': PaastaColors.cyan,
        'help': 'Output from the paasta deploy code. (setup_marathon_job, bounces, etc)',
        'command': 'NA - TODO: tee deploy logs into scribe PAASTA-201',
    },
    'app_output': {
        'color': PaastaColors.bold,
        'help': 'Stderr and stdout of the actual process spawned by Mesos',
        'command': 'NA - PAASTA-78',
    },
    'app_request': {
        'color': PaastaColors.bold,
        'help': 'The request log for the service. Defaults to "service_NAME_requests"',
        'command': 'scribe_reader -e ENV -f service_example_happyhour_requests',
    },
    'app_errors': {
        'color': PaastaColors.red,
        'help': 'Application error log, defaults to "stream_service_NAME_errors"',
        'command': 'scribe_reader -e ENV -f stream_service_SERVICE_errors',
    },
    'lb_requests': {
        'color': PaastaColors.bold,
        'help': 'All requests from Smartstack haproxy',
        'command': 'NA - TODO: SRV-1130',
    },
    'lb_errors': {
        'color': PaastaColors.red,
        'help': 'Logs from Smartstack haproxy that have 400-500 error codes',
        'command': 'scribereader -e ENV -f stream_service_errors | grep SERVICE.instance',
    },
    'monitoring': {
        'color': PaastaColors.green,
        'help': 'Logs from Sensu checks for the service',
        'command': 'NA - TODO log mesos healthcheck and sensu stuff.',
    },
}


class NoSuchLogComponent(Exception):
    pass


def validate_log_component(component):
    if component in LOG_COMPONENTS.keys():
        return True
    else:
        raise NoSuchLogComponent


def get_git_url(service):
    """Get the git url for a service. Assumes that the service's
    repo matches its name, and that it lives in services- i.e.
    if this is called with the string 'test', the returned
    url will be git@git.yelpcorp.com:services/test.git.

    :param service: The service name to get a URL for
    :returns: A git url to the service's repository"""
    return 'git@git.yelpcorp.com:services/%s.git' % service


class NoSuchLogLevel(Exception):
    pass


def configure_log():
    """We will log to the yocalhost binded scribe."""
    clog.config.configure(scribe_host='169.254.255.254', scribe_port=1463, scribe_disable=False)


def _now():
    return datetime.datetime.utcnow().isoformat()


def remove_ansi_escape_sequences(line):
    """Removes ansi escape sequences from the given line."""
    return no_escape.sub('', line)


def format_log_line(level, cluster, instance, component, line):
    """Accepts a string 'line'.

    Returns an appropriately-formatted dictionary which can be serialized to
    JSON for logging and which contains 'line'.
    """
    validate_log_component(component)
    now = _now()
    line = remove_ansi_escape_sequences(line)
    message = json.dumps({
        'timestamp': now,
        'level': level,
        'cluster': cluster,
        'instance': instance,
        'component': component,
        'message': line,
    }, sort_keys=True)
    return message


def get_log_name_for_service(service_name):
    return 'stream_paasta_%s' % service_name


def _log(service_name, line, component, level=DEFAULT_LOGLEVEL, cluster=ANY_CLUSTER, instance=ANY_INSTANCE):
    """This expects someone (currently the paasta cli main()) to have already
    configured the log object. We'll just write things to it.
    """
    if level == 'event':
        print(line, file=sys.stdout)
    elif level == 'debug':
        print(line, file=sys.stderr)
    else:
        raise NoSuchLogLevel
    log_name = get_log_name_for_service(service_name)
    formatted_line = format_log_line(level, cluster, instance, component, line)
    clog.log_line(log_name, formatted_line)


def _timeout(process):
    """Helper function for _run. It terminates the process.
    Doesn't raise OSError, if we try to terminate a non-existing
    process as there can be a very small window between poll() and kill()
    """
    if process.poll() is None:
        try:
            # sending SIGKILL to the process
            process.kill()
        except OSError as e:
            # No such process error
            # The process could have been terminated meanwhile
            if e.errno != errno.ESRCH:
                raise


class PaastaNotConfigured(Exception):
    pass


class NoMarathonClusterFoundException(Exception):
    pass


def load_system_paasta_config(path=PATH_TO_SYSTEM_PAASTA_CONFIG):
    """
    Read Marathon configs to get cluster info and volumes
    that we need to bind when runngin a container.
    """
    try:
        with open(path) as f:
            return json.loads(f)
    except IOError as e:
        raise PaastaNotConfigured("Could not load system paasta config file %s: %s" % (e.filename, e.strerror))


class SystemPaastaConfig(dict):

    log = logging.getLogger('__main__')

    def get_cluster(self):
        """Get the cluster defined in this host's marathon config file.

        :returns: The name of the cluster defined in the marathon configuration"""
        try:
            return self['cluster']
        except KeyError:
            self.log.warning('Could not find marathon cluster in marathon config at %s' % PATH_TO_SYSTEM_PAASTA_CONFIG)
            raise NoMarathonClusterFoundException


def _run(command, env=os.environ, timeout=None, log=False, **kwargs):
    """Given a command, run it. Return a tuple of the return code and any
    output.

    :param timeout: If specified, the command will be terminated after timeout
        seconds.
    :param log: If True, the _log will be handled by _run. If set, it is mandatory
        to pass at least a :service_name: and a :component: parameter. Optionally you
        can pass :cluster:, :instance: and :loglevel: parameters for logging.
    We wanted to use plumbum instead of rolling our own thing with
    subprocess.Popen but were blocked by
    https://github.com/tomerfiliba/plumbum/issues/162 and our local BASH_FUNC
    magic.
    """
    output = []
    if log:
        service_name = kwargs['service_name']
        component = kwargs['component']
        cluster = kwargs.get('cluster', ANY_CLUSTER)
        instance = kwargs.get('instance', ANY_INSTANCE)
        loglevel = kwargs.get('loglevel', DEFAULT_LOGLEVEL)
    try:
        process = Popen(shlex.split(command), stdout=PIPE, stderr=STDOUT, env=env)
        process.name = command
        # start the timer if we specified a timeout
        if timeout:
            proctimer = threading.Timer(timeout, _timeout, (process,))
            proctimer.start()
        for line in iter(process.stdout.readline, ''):
            if log:
                _log(
                    service_name=service_name,
                    line=line.rstrip('\n'),
                    component=component,
                    level=loglevel,
                    cluster=cluster,
                    instance=instance,
                )
            output.append(line.rstrip('\n'))
        # when finished, get the exit code
        returncode = process.wait()
    except OSError as e:
        if log:
            _log(
                service_name=service_name,
                line=e.strerror.rstrip('\n'),
                component=component,
                level=loglevel,
                cluster=cluster,
                instance=instance,
            )
        output.append(e.strerror.rstrip('\n'))
        returncode = e.errno
    # Stop the timer
    if timeout:
        proctimer.cancel()
    if returncode == -9:
        output.append("Command '%s' timed out (longer than %ss)" % (command, timeout))
    return returncode, '\n'.join(output)


def get_umask():
    """Get the current umask for this process. NOT THREAD SAFE."""
    old_umask = os.umask(0022)
    os.umask(old_umask)
    return old_umask


@contextlib.contextmanager
def atomic_file_write(target_path):
    dirname = os.path.dirname(target_path)
    basename = os.path.basename(target_path)

    with tempfile.NamedTemporaryFile(
        dir=dirname,
        prefix=('.%s-' % basename),
        delete=False
    ) as f:
        temp_target_path = f.name
        yield f

    mode = 0666 & (~get_umask())
    os.chmod(temp_target_path, mode)
    os.rename(temp_target_path, target_path)


def build_docker_image_name(upstream_job_name):
    """docker-paasta.yelpcorp.com:443 is the URL for the Registry where PaaSTA
    will look for your images.

    upstream_job_name is a sanitized-for-Jenkins (s,/,-,g) version of the
    service's path in git. E.g. For git.yelpcorp.com:services/foo the
    upstream_job_name is services-foo.
    """
    name = 'docker-paasta.yelpcorp.com:443/services-%s' % upstream_job_name
    return name


def build_docker_tag(upstream_job_name, upstream_git_commit):
    """Builds the DOCKER_TAG string

    upstream_job_name is a sanitized-for-Jenkins (s,/,-,g) version of the
    service's path in git. E.g. For git.yelpcorp.com:services/foo the
    upstream_job_name is services-foo.

    upstream_git_commit is the SHA that we're building. Usually this is the
    tip of origin/master.
    """
    tag = '%s:paasta-%s' % (
        build_docker_image_name(upstream_job_name),
        upstream_git_commit,
    )
    return tag


def check_docker_image(service_name, tag):
    """Checks whether the given image for :service_name: with :tag: exists.
    Returns True if there is exactly one matching image found.
    Raises ValueError if more than one docker image with :tag: found.
    """
    docker_client = docker.Client(timeout=60)
    image_name = build_docker_image_name(service_name)
    docker_tag = build_docker_tag(service_name, tag)
    images = docker_client.images(name=image_name)
    result = [image for image in images if docker_tag in image['RepoTags']]
    if len(result) > 1:
        raise ValueError('More than one docker image found with tag %s\n%s' % docker_tag, result)
    return len(result) == 1


def datetime_from_utc_to_local(utc_datetime):
    local_tz = dateutil.tz.tzlocal()
    # We make out datetime timezone aware
    utc_datetime = utc_datetime.replace(tzinfo=dateutil.tz.tzutc())
    # We convert to the local timezone
    local_datetime = utc_datetime.astimezone(local_tz)
    # We need to remove timezone awareness because of humanize
    local_datetime = local_datetime.replace(tzinfo=None)
    return local_datetime


def get_username():
    """Returns the current username in a portable way
    http://stackoverflow.com/a/2899055
    """
    return pwd.getpwuid(os.getuid())[0]


def list_all_clusters(zk_discovery_path=INFRA_ZK_PATH):
    """Returns a set of all infrastructure zookeeper clusters.
    This makes the assumption that paasta clusters and zookeeper
    clusters are the same"""
    clusters = set()
    for yaml_file in os.listdir(zk_discovery_path):
        clusters.add(yaml_file.split('.')[0])
    return clusters


def parse_yaml_file(yaml_file):
    return yaml.load(open(yaml_file))


def get_infrastructure_zookeeper_servers(cluster, zk_discovery_path=INFRA_ZK_PATH):
    """Reads a yelp zookeeper toplogy file for a given cluster and returns
    a list of the zookeeper server ips"""
    yaml_file = os.path.join(zk_discovery_path, "%s%s" % (cluster, '.yaml'))
    cluster_topology = parse_yaml_file(yaml_file)
    return [host_port[0] for host_port in cluster_topology]
