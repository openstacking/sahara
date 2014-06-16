# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import uuid

from oslo.config import cfg
import six

from sahara import conductor as c
from sahara import context
from sahara.openstack.common import log
from sahara.plugins import base as plugin_base
from sahara.service.edp.binary_retrievers import dispatch
from sahara.service.edp import hdfs_helper as h
from sahara.service.edp import oozie as o
from sahara.service.edp.workflow_creator import workflow_factory
from sahara.utils import edp
from sahara.utils import remote
from sahara.utils import xmlutils as x


LOG = log.getLogger(__name__)

opts = [
    cfg.StrOpt('job_workflow_postfix',
               default='',
               help='Postfix for storing jobs in hdfs. Will be '
                    'added to /user/hadoop/.')
]

CONF = cfg.CONF
CONF.register_opts(opts)

conductor = c.API

terminated_job_states = ['DONEWITHERROR', 'FAILED', 'KILLED', 'SUCCEEDED']


def get_job_status(job_execution_id):
    ctx = context.ctx()
    job_execution = conductor.job_execution_get(ctx, job_execution_id)
    if job_execution.oozie_job_id is None:
        # We don't have an Oozie id yet for this job, that's okay
        return job_execution

    cluster = conductor.cluster_get(ctx, job_execution.cluster_id)

    if cluster is None or cluster.status != 'Active':
        return job_execution

    client = _create_oozie_client(cluster)
    job_info = client.get_job_status(job_execution)
    update = {"info": job_info}
    if job_info['status'] in terminated_job_states:
        update['end_time'] = datetime.datetime.now()

    job_execution = conductor.job_execution_update(ctx, job_execution,
                                                   update)
    return job_execution


def update_job_statuses():
    ctx = context.ctx()
    for je in conductor.job_execution_get_all(ctx, end_time=None):
        try:
            get_job_status(je.id)
        except Exception as e:
            LOG.exception("Error during update job execution %s: %s" %
                          (je.id, e))


def _get_plugin(cluster):
    return plugin_base.PLUGINS.get_plugin(cluster.plugin_name)


def _create_oozie_client(cluster):
    plugin = _get_plugin(cluster)
    return o.OozieClient(plugin.get_oozie_server_uri(cluster),
                         plugin.get_oozie_server(cluster))


def cancel_job(job_execution_id):
    ctx = context.ctx()
    job_execution = conductor.job_execution_get(ctx, job_execution_id)
    cluster = conductor.cluster_get(ctx, job_execution.cluster_id)

    client = _create_oozie_client(cluster)
    client.kill_job(job_execution)

    job_info = client.get_job_status(job_execution)
    update = {"info": job_info,
              "end_time": datetime.datetime.now()}
    job_execution = conductor.job_execution_update(ctx, job_execution,
                                                   update)

    return job_execution


def _update_job_execution_extra(job_execution, cluster):
    if CONF.use_namespaces and not CONF.use_floating_ips:
        oozie = _get_plugin(cluster).get_oozie_server(cluster)
        info = oozie.remote().get_neutron_info()
        extra = job_execution.extra.copy()
        extra['neutron'] = info

        job_execution = conductor.job_execution_update(
            context.ctx(), job_execution.id, {'extra': extra})
    return job_execution


def _get_data_sources(job_execution, job):
    if edp.compare_job_type(job.type, edp.JOB_TYPE_JAVA):
        return None, None

    ctx = context.ctx()
    input_source = conductor.data_source_get(ctx, job_execution.input_id)
    output_source = conductor.data_source_get(ctx, job_execution.output_id)
    return input_source, output_source


def _get_oozie_job_params(cluster, hdfs_user, path_to_workflow):
    plugin = _get_plugin(cluster)
    rm_path = plugin.get_resource_manager_uri(cluster)
    nn_path = plugin.get_name_node_uri(cluster)
    job_parameters = {
        "jobTracker": rm_path,
        "nameNode": nn_path,
        "user.name": hdfs_user,
        "oozie.wf.application.path": "%s%s" % (nn_path, path_to_workflow),
        "oozie.use.system.libpath": "true"}
    return job_parameters


def run_job(job_execution_id):
    try:
        _run_job(job_execution_id)
    except Exception as ex:
        LOG.exception("Can't run job execution '%s' (reason: %s)",
                      job_execution_id, ex)

        conductor.job_execution_update(
            context.ctx(), job_execution_id,
            {'info': {'status': 'FAILED'},
             'start_time': datetime.datetime.now(),
             'end_time': datetime.datetime.now()})


def _run_job(job_execution_id):
    ctx = context.ctx()

    job_execution = conductor.job_execution_get(ctx, job_execution_id)

    cluster = conductor.cluster_get(ctx, job_execution.cluster_id)
    if cluster.status != 'Active':
        return

    job_execution = _update_job_execution_extra(job_execution, cluster)

    job = conductor.job_get(ctx, job_execution.job_id)
    input_source, output_source = _get_data_sources(job_execution, job)

    for data_source in [input_source, output_source]:
        if data_source and data_source.type == 'hdfs':
            h.configure_cluster_for_hdfs(cluster, data_source)

    plugin = _get_plugin(cluster)
    hdfs_user = plugin.get_hdfs_user()
    oozie_server = plugin.get_oozie_server(cluster)

    wf_dir = create_workflow_dir(oozie_server, job, hdfs_user)
    upload_job_files(oozie_server, wf_dir, job, hdfs_user)

    wf_xml = workflow_factory.get_workflow_xml(
        job, cluster, job_execution, input_source, output_source)

    path_to_workflow = upload_workflow_file(oozie_server,
                                            wf_dir, wf_xml, hdfs_user)

    client = _create_oozie_client(cluster)
    job_params = _get_oozie_job_params(cluster, hdfs_user, path_to_workflow)
    oozie_job_id = client.add_job(x.create_hadoop_xml(job_params),
                                  job_execution)
    job_execution = conductor.job_execution_update(
        ctx, job_execution, {'oozie_job_id': oozie_job_id,
                             'start_time': datetime.datetime.now()})
    client.run_job(job_execution, oozie_job_id)


def upload_job_files(where, job_dir, job, hdfs_user):
    mains = job.mains or []
    libs = job.libs or []
    uploaded_paths = []

    with remote.get_remote(where) as r:
        for main in mains:
            raw_data = dispatch.get_raw_binary(main)
            h.put_file_to_hdfs(r, raw_data, main.name, job_dir, hdfs_user)
            uploaded_paths.append(job_dir + '/' + main.name)
        for lib in libs:
            raw_data = dispatch.get_raw_binary(lib)
            # HDFS 2.2.0 fails to put file if the lib dir does not exist
            h.create_dir(r, job_dir + "/lib", hdfs_user)
            h.put_file_to_hdfs(r, raw_data, lib.name, job_dir + "/lib",
                               hdfs_user)
            uploaded_paths.append(job_dir + '/lib/' + lib.name)
    return uploaded_paths


def upload_workflow_file(where, job_dir, wf_xml, hdfs_user):
    with remote.get_remote(where) as r:
        h.put_file_to_hdfs(r, wf_xml, "workflow.xml", job_dir, hdfs_user)

    return "%s/workflow.xml" % job_dir


def create_workflow_dir(where, job, hdfs_user):
    constructed_dir = '/user/%s/' % hdfs_user
    constructed_dir = _add_postfix(constructed_dir)
    constructed_dir += '%s/%s' % (job.name, six.text_type(uuid.uuid4()))
    with remote.get_remote(where) as r:
        h.create_dir(r, constructed_dir, hdfs_user)

    return constructed_dir


def _add_postfix(constructed_dir):
    constructed_dir = _append_slash_if_needed(constructed_dir)
    if CONF.job_workflow_postfix:
        constructed_dir = ''.join([str(constructed_dir),
                                   str(CONF.job_workflow_postfix)])
    return _append_slash_if_needed(constructed_dir)


def _append_slash_if_needed(path):
    if path[-1] != '/':
        path += '/'
    return path
