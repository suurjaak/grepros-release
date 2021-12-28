#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test: grep input bags to SQL schema output.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     22.12.2021
@modified    24.12.2021
------------------------------------------------------------------------------
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test import testbase

logger = logging.getLogger()


class TestBagInputSqlOutput(testbase.TestBase):
    """Tests grepping from input bags and writing schema to SQL file."""

    ## Test name used in flow logging
    NAME = os.path.splitext(os.path.basename(__file__))[0]

    ## Name used in logging
    OUTPUT_LABEL = "SQL"

    ## Suffix for write output file
    OUTPUT_SUFFIX = ".sql"

    def setUp(self):
        """Collects bags in data directory, assembles command."""
        super().setUp()
        self._cmd = self.CMD_BASE + ["--no-console-output", "--plugin", "grepros.plugins.sql",
                                     "--write", self._outname]

    def test_grepros(self):
        """Runs grepros on bags in data directory, verifies HTML output."""
        self.verify_bags()
        self.run_command()
        self.assertTrue(os.path.isfile(self._outname), "Expected output file not written.")

        logger.info("Reading data from written %s.", self.OUTPUT_LABEL)
        with open(self._outname) as f:
            fulltext = f.read()
        self.verify_topics(fulltext)


if "__main__" == __name__:
    TestBagInputSqlOutput.run_rostest()
