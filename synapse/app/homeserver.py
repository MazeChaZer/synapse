#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2014 matrix.org
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
# limitations under the License.

from synapse.storage import read_schema

from synapse.server import HomeServer

from twisted.internet import reactor
from twisted.enterprise import adbapi
from twisted.web.resource import Resource
from twisted.web.static import File
from twisted.web.server import Site
from synapse.http.server import JsonResource, RootRedirect, ContentRepoResource
from synapse.http.client import TwistedHttpClient
from synapse.api.urls import (
    CLIENT_PREFIX, FEDERATION_PREFIX, WEB_CLIENT_PREFIX, CONTENT_REPO_PREFIX
)
from synapse.config.homeserver import HomeServerConfig
from synapse.crypto import context_factory

from daemonize import Daemonize
import twisted.manhole.telnet

import logging
import sqlite3
import os
import re
import sys

logger = logging.getLogger(__name__)


SCHEMAS = [
    "transactions",
    "pdu",
    "users",
    "profiles",
    "presence",
    "im",
    "room_aliases",
]


# Remember to update this number every time an incompatible change is made to
# database schema files, so the users will be informed on server restarts.
SCHEMA_VERSION = 1


class SynapseHomeServer(HomeServer):

    def build_http_client(self):
        return TwistedHttpClient()

    def build_resource_for_client(self):
        return JsonResource()

    def build_resource_for_federation(self):
        return JsonResource()

    def build_resource_for_web_client(self):
        return File("webclient")  # TODO configurable?

    def build_resource_for_content_repo(self):
        return ContentRepoResource(self, self.upload_dir, self.auth)

    def build_db_pool(self):
        """ Set up all the dbs. Since all the *.sql have IF NOT EXISTS, so we
        don't have to worry about overwriting existing content.
        """
        logging.info("Preparing database: %s...", self.db_name)

        with sqlite3.connect(self.db_name) as db_conn:
            c = db_conn.cursor()
            c.execute("PRAGMA user_version")
            row = c.fetchone()

            if row and row[0]:
                user_version = row[0]

                if user_version < SCHEMA_VERSION:
                    # TODO(paul): add some kind of intelligent fixup here
                    raise ValueError("Cannot use this database as the " +
                        "schema version (%d) does not match (%d)" %
                        (user_version, SCHEMA_VERSION)
                    )

            else:
                for sql_loc in SCHEMAS:
                    sql_script = read_schema(sql_loc)

                    c.executescript(sql_script)
                    db_conn.commit()

                c.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

            c.close()

        logging.info("Database prepared in %s.", self.db_name)

        pool = adbapi.ConnectionPool(
            'sqlite3', self.db_name, check_same_thread=False,
            cp_min=1, cp_max=1)

        return pool

    def create_resource_tree(self, web_client, redirect_root_to_web_client):
        """Create the resource tree for this Home Server.

        This in unduly complicated because Twisted does not support putting
        child resources more than 1 level deep at a time.

        Args:
            web_client (bool): True to enable the web client.
            redirect_root_to_web_client (bool): True to redirect '/' to the
            location of the web client. This does nothing if web_client is not
            True.
        """
        # list containing (path_str, Resource) e.g:
        # [ ("/aaa/bbb/cc", Resource1), ("/aaa/dummy", Resource2) ]
        desired_tree = [
            (CLIENT_PREFIX, self.get_resource_for_client()),
            (FEDERATION_PREFIX, self.get_resource_for_federation()),
            (CONTENT_REPO_PREFIX, self.get_resource_for_content_repo())
        ]
        if web_client:
            logger.info("Adding the web client.")
            desired_tree.append((WEB_CLIENT_PREFIX,
                                self.get_resource_for_web_client()))

        if web_client and redirect_root_to_web_client:
            self.root_resource = RootRedirect(WEB_CLIENT_PREFIX)
        else:
            self.root_resource = Resource()

        # ideally we'd just use getChild and putChild but getChild doesn't work
        # unless you give it a Request object IN ADDITION to the name :/ So
        # instead, we'll store a copy of this mapping so we can actually add
        # extra resources to existing nodes. See self._resource_id for the key.
        resource_mappings = {}
        for (full_path, resource) in desired_tree:
            logging.info("Attaching %s to path %s", resource, full_path)
            last_resource = self.root_resource
            for path_seg in full_path.split('/')[1:-1]:
                if not path_seg in last_resource.listNames():
                    # resource doesn't exist, so make a "dummy resource"
                    child_resource = Resource()
                    last_resource.putChild(path_seg, child_resource)
                    res_id = self._resource_id(last_resource, path_seg)
                    resource_mappings[res_id] = child_resource
                    last_resource = child_resource
                else:
                    # we have an existing Resource, use that instead.
                    res_id = self._resource_id(last_resource, path_seg)
                    last_resource = resource_mappings[res_id]

            # ===========================
            # now attach the actual desired resource
            last_path_seg = full_path.split('/')[-1]

            # if there is already a resource here, thieve its children and
            # replace it
            res_id = self._resource_id(last_resource, last_path_seg)
            if res_id in resource_mappings:
                # there is a dummy resource at this path already, which needs
                # to be replaced with the desired resource.
                existing_dummy_resource = resource_mappings[res_id]
                for child_name in existing_dummy_resource.listNames():
                    child_res_id = self._resource_id(existing_dummy_resource,
                                                     child_name)
                    child_resource = resource_mappings[child_res_id]
                    # steal the children
                    resource.putChild(child_name, child_resource)

            # finally, insert the desired resource in the right place
            last_resource.putChild(last_path_seg, resource)
            res_id = self._resource_id(last_resource, last_path_seg)
            resource_mappings[res_id] = resource

        return self.root_resource

    def _resource_id(self, resource, path_seg):
        """Construct an arbitrary resource ID so you can retrieve the mapping
        later.

        If you want to represent resource A putChild resource B with path C,
        the mapping should looks like _resource_id(A,C) = B.

        Args:
            resource (Resource): The *parent* Resource
            path_seg (str): The name of the child Resource to be attached.
        Returns:
            str: A unique string which can be a key to the child Resource.
        """
        return "%s-%s" % (resource, path_seg)

    def start_listening(self, port):
        reactor.listenSSL(
            port, Site(self.root_resource), self.tls_context_factory
        )
        logger.info("Synapse now listening on port %d", port)


def run():
    reactor.run()


def setup():
    config = HomeServerConfig.load_config(
        "Synapse Homeserver",
        sys.argv[1:],
        generate_section="Homeserver"
    )

    config.setup_logging()

    logger.info("Server hostname: %s", config.server_name)

    if re.search(":[0-9]+$", config.server_name):
        domain_with_port = config.server_name
    else:
        domain_with_port = "%s:%s" % (config.server_name, config.bind_port)

    tls_context_factory = context_factory.ServerContextFactory(config)

    hs = SynapseHomeServer(
        config.server_name,
        domain_with_port=domain_with_port,
        upload_dir=os.path.abspath("uploads"),
        db_name=config.database_path,
        tls_context_factory=tls_context_factory,
    )

    hs.register_servlets()

    hs.create_resource_tree(
        web_client=config.webclient,
        redirect_root_to_web_client=True,
    )
    hs.start_listening(config.bind_port)

    hs.get_db_pool()

    if config.manhole:
        f = twisted.manhole.telnet.ShellFactory()
        f.username = "matrix"
        f.password = "rabbithole"
        f.namespace['hs'] = hs
        reactor.listenTCP(config.manhole, f, interface='127.0.0.1')

    if config.daemonize:
        print config.pid_file
        daemon = Daemonize(
            app="synapse-homeserver",
            pid=config.pid_file,
            action=run,
            auto_close_fds=False,
            verbose=True,
            logger=logger,
        )

        daemon.start()
    else:
        run()


if __name__ == '__main__':
    setup()
