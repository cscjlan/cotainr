"""
cotainr - a user space Apptainer/Singularity container builder.

Copyright DeiC, deic.dk
Licensed under the European Union Public License (EUPL) 1.2
- see the LICENSE file for details.

This module implements the interaction with the container runtime.

Classes
-------
SingularitySandbox
    A Singularity container sandbox context manager.
"""

import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory

from . import __version__ as _cotainr_version
from . import tracing
from . import util

logger = logging.getLogger(__name__)


class SingularitySandbox:
    """
    A Singularity container sandbox context manager.

    This creates and manipulates a `Singularity sandbox
    <http://apptainer.org/docs/user/main/build_a_container.html#creating-writable-sandbox-directories>`_,
    i.e. a temporary directory representing the container. As a final step, the
    sandbox should be converted into a SIF container image file.

    Parameters
    ----------
    base_image : str
        Base image to use for the container which may be any valid
        Apptainer/Singularity <BUILD SPEC>.

    Attributes
    ----------
    base_image : str
        Base image to use for the container.
    sandbox_dir : :class:`os.PathLike` or None
        The path to the temporary directory containing the sandbox if within a
        sandbox context, otherwise it is None.
    """

    def __init__(self, *, base_image, log_settings=None):
        """Construct the SingularitySandbox context manager."""
        self.base_image = base_image
        self.sandbox_dir = None
        if log_settings is not None:
            self.verbosity = log_settings.verbosity
            self.log_dispatcher = tracing.LogDispatcher(
                name=__class__.__name__,
                map_log_level_func=self._map_log_level,
                log_settings=log_settings,
            )
        else:
            self.verbosity = 0
            self.log_dispatcher = None

    def __enter__(self):
        """
        Build and enter sandbox context.

        Returns
        -------
        self : :class:`SingularitySandbox`
            The sandbox context.
        """
        # Store current directory
        self._origin = Path().resolve()

        # Create sandbox
        self._tmp_dir = TemporaryDirectory()
        self.sandbox_dir = Path(self._tmp_dir.name) / "singularity_sandbox"
        self.sandbox_dir.mkdir(exist_ok=False)
        self._subprocess_runner(
            args=self._add_verbosity_arg(
                args=[
                    "singularity",
                    "--nocolor",
                    "build",
                    "--force",  # sandbox_dir.mkdir() checks for existing sandbox image
                    "--sandbox",
                    self.sandbox_dir,
                    self.base_image,
                ]
            ),
        )

        # Change directory to the sandbox
        os.chdir(self.sandbox_dir)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Exit and destroy sandbox context."""
        os.chdir(self._origin)
        self._tmp_dir.cleanup()
        self.sandbox_dir = None

    def add_metadata(self):
        """
        Add metadata to the container sandbox.

        The following metadata is added to the container sandbox:
          - "cotainr.command": The full command line used to build the container.
          - "cotainr.version": The version of cotainr used to build the container.
          - "cotainr.url": The cotainr project url.

        The container metadata may be inspected by running `singularity inspect` on the
        built container image file.

        Notes
        -----
        The metadata entries are added to the `.singularity.d/labels.json
        <https://apptainer.org/docs/user/main/environment_and_metadata.html#singularity-d-directory>`_
        file.

        """
        labels_path = self.sandbox_dir / ".singularity.d/labels.json"
        with open(labels_path, "r+") as f:
            metadata = json.load(f)
            metadata["cotainr.command"] = " ".join(sys.argv)
            metadata["cotainr.version"] = _cotainr_version
            metadata["cotainr.url"] = "https://github.com/DeiC-HPC/cotainr"
            f.seek(0)
            json.dump(metadata, f)

    def add_to_env(self, *, shell_script):
        """
        Add `shell_script` to the sourced environment in the container.

        The content of `shell_script` is written as-is to the /environment file
        in the Singularity container which is sourced on execution of the
        container.

        Parameters
        ----------
        shell_script : str
            The shell script to add to the sourced environment in the
            container.
        """
        self._assert_within_sandbox_context()

        env_file = self.sandbox_dir / "environment"
        with env_file.open(mode="a") as f:
            f.write(shell_script + "\n")

    def build_image(self, *, path):
        """
        Build a SIF image file from sandbox.

        Takes the current content of the sandbox and builds a SIF container
        image from it. The container image is outputted to `path`.

        Parameters
        ----------
        path : :class:`os.PathLike`
            Path to the built container image.
        """
        self._assert_within_sandbox_context()

        self._subprocess_runner(
            args=self._add_verbosity_arg(
                args=[
                    "singularity",
                    "--nocolor",
                    "build",
                    "--force",
                    path,
                    self.sandbox_dir,
                ]
            ),
        )

    def run_command_in_container(self, *, cmd, custom_log_dispatcher=None):
        """
        Run a command in the container sandbox.

        Wraps `singularity exec` of the `cmd` in the container sandbox`
        allowing for running commands inside the container sandbox context,
        e.g. for installing software in the container sandbox.

        Parameters
        ----------
        cmd : str
            The command to run in the container sandbox.

        Returns
        -------
        process : :class:`subprocess.CompletedProcess`
            Information about the process that ran in the container sandbox.

        Notes
        -----
        We pass several flags to the `singularity exec` command to provide
        maximum compatibility with different HPC systems. In particular, we
        use:

        - `--no-home` as trying to mount the home folder on some systems (e.g.
          LUMI) causes problems. Thus, when running a command in the container,
          you cannot reference files in your home directory. Instead you must
          copy all files into the container sandbox and then reference the
          files relative to the container root.
        - `--no-umask` as some systems use a default umask (e.g. 0007 on LUMI)
          that prevents you from accessing any files added to the container as
          a regular user when you run the built container, e.g. such files are
          owned by root:root with 660 permissions for a 0007 umask. Thus, all
          files added to the container by running a command in the container
          will have file permissions 644 (Apptainer/Singularity forces the
          umask to 0022). If you need other file permissions, you must manually
          change them.
        """
        self._assert_within_sandbox_context()

        try:
            process = self._subprocess_runner(
                custom_log_dispatcher=custom_log_dispatcher,
                args=self._add_verbosity_arg(
                    args=[
                        "singularity",
                        "--nocolor",
                        "exec",
                        "--writable",
                        "--no-home",
                        "--no-umask",
                        self.sandbox_dir,
                        *shlex.split(cmd),
                    ]
                ),
            )
        except subprocess.CalledProcessError as e:
            singularity_fatal_error = "\n".join(
                [line for line in e.stderr.split("\n") if line.startswith("FATAL")]
            )
            raise ValueError(
                f"Invalid command {cmd=} passed to Singularity "
                f"resulted in the FATAL error: {singularity_fatal_error}"
            ) from e

        return process

    def _assert_within_sandbox_context(self):
        """Raise a ValueError if we are not inside the sandbox context."""
        if self.sandbox_dir is None:
            raise ValueError("The operation is only valid inside a sandbox context.")

    def _add_verbosity_arg(self, *, args):
        if self.verbosity <= 0:
            args.insert(1, "--quiet")
        elif self.verbosity >= 3:
            # Assume --verbose is a debug level
            args.insert(1, "--verbose")

        return args

    def _subprocess_runner(self, *, custom_log_dispatcher=None, args, **kwargs):
        """Wrap the choice of subprocess runner."""
        if custom_log_dispatcher is not None:
            with custom_log_dispatcher.prefix_stderr_name(
                prefix=self.__class__.__name__
            ):
                return util.stream_subprocess(
                    log_dispatcher=custom_log_dispatcher, args=args, **kwargs
                )
        else:
            return util.stream_subprocess(
                log_dispatcher=self.log_dispatcher, args=args, **kwargs
            )

    @staticmethod
    def _map_log_level(msg):
        if msg.startswith("DEBUG") or msg.startswith("VERBOSE"):
            return logging.DEBUG
        elif msg.startswith("INFO") or msg.startswith("LOG"):
            return logging.INFO
        elif msg.startswith("WARNING"):
            return logging.WARNING
        elif msg.startswith("ERROR"):
            return logging.ERROR
        elif msg.startswith("ABRT") or msg.startswith("FATAL"):
            return logging.CRITICAL
        else:
            # If no prefix on message, assume its INFO level
            return logging.INFO
