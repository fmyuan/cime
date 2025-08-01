"""
A library for scheduling/running through the phases of a set
of system tests. Supports phase-level parallelism (can make progres
on multiple system tests at once).

TestScheduler will handle the TestStatus for the 1-time setup
phases. All other phases need to handle their own status because
they can be run outside the context of TestScheduler.
"""

import os
import traceback, stat, threading, time, glob
from collections import OrderedDict

from CIME.XML.standard_module_setup import *
from CIME.get_tests import get_recommended_test_time, get_build_groups, is_perf_test
from CIME.status import append_status, append_testlog
from CIME.utils import (
    TESTS_FAILED_ERR_CODE,
    parse_test_name,
    get_full_test_name,
    get_model,
    convert_to_seconds,
    get_cime_root,
    get_src_root,
    get_tools_path,
    get_template_path,
    get_project,
    get_timestamp,
    get_cime_default_driver,
    clear_folder,
    CIMEError,
)
from CIME.config import Config
from CIME.test_status import *
from CIME.XML.machines import Machines
from CIME.XML.generic_xml import GenericXML
from CIME.XML.env_test import EnvTest
from CIME.XML.env_mach_pes import EnvMachPes
from CIME.XML.files import Files
from CIME.XML.component import Component
from CIME.XML.tests import Tests
from CIME.case import Case
from CIME.wait_for_tests import wait_for_tests
from CIME.provenance import get_recommended_test_time_based_on_past
from CIME.locked_files import lock_file
from CIME.cs_status_creator import create_cs_status
from CIME.hist_utils import generate_teststatus
from CIME.build import post_build
from CIME.SystemTests.test_mods import find_test_mods

logger = logging.getLogger(__name__)

# Phases managed by TestScheduler
TEST_START = "INIT"  # Special pseudo-phase just for test_scheduler bookkeeping
PHASES = [
    TEST_START,
    CREATE_NEWCASE_PHASE,
    XML_PHASE,
    SETUP_PHASE,
    SHAREDLIB_BUILD_PHASE,
    MODEL_BUILD_PHASE,
    RUN_PHASE,
]  # Order matters

###############################################################################
def _translate_test_names_for_new_pecount(test_names, force_procs, force_threads):
    ###############################################################################
    new_test_names = []
    caseopts = []
    for test_name in test_names:
        (
            testcase,
            caseopts,
            grid,
            compset,
            machine,
            compiler,
            testmods,
        ) = parse_test_name(test_name)
        rewrote_caseopt = False
        if caseopts is not None:
            for idx, caseopt in enumerate(caseopts):
                if caseopt.startswith("P"):
                    caseopt = caseopt[1:]
                    if "x" in caseopt:
                        old_procs, old_thrds = caseopt.split("x")
                    else:
                        old_procs, old_thrds = caseopt, None

                    new_procs = force_procs if force_procs is not None else old_procs
                    new_thrds = (
                        force_threads if force_threads is not None else old_thrds
                    )

                    newcaseopt = (
                        ("P{}".format(new_procs))
                        if new_thrds is None
                        else ("P{}x{}".format(new_procs, new_thrds))
                    )
                    caseopts[idx] = newcaseopt

                    rewrote_caseopt = True
                    break

        if not rewrote_caseopt:
            force_procs = "M" if force_procs is None else force_procs
            newcaseopt = (
                ("P{}".format(force_procs))
                if force_threads is None
                else ("P{}x{}".format(force_procs, force_threads))
            )
            if caseopts is None:
                caseopts = [newcaseopt]
            else:
                caseopts.append(newcaseopt)

        new_test_name = get_full_test_name(
            testcase,
            caseopts=caseopts,
            grid=grid,
            compset=compset,
            machine=machine,
            compiler=compiler,
            testmods_list=testmods,
        )
        new_test_names.append(new_test_name)

    return new_test_names


_TIME_CACHE = {}
###############################################################################
def _get_time_est(test, baseline_root, as_int=False, use_cache=False, raw=False):
    ###############################################################################
    if test in _TIME_CACHE and use_cache:
        return _TIME_CACHE[test]

    recommended_time = get_recommended_test_time_based_on_past(
        baseline_root, test, raw=raw
    )

    if recommended_time is None:
        recommended_time = get_recommended_test_time(test)

    if as_int:
        if recommended_time is None:
            recommended_time = 9999999999
        else:
            recommended_time = convert_to_seconds(recommended_time)

    if use_cache:
        _TIME_CACHE[test] = recommended_time

    return recommended_time


###############################################################################
def _order_tests_by_runtime(tests, baseline_root):
    ###############################################################################
    tests.sort(
        key=lambda x: _get_time_est(
            x, baseline_root, as_int=True, use_cache=True, raw=True
        ),
        reverse=True,
    )


###############################################################################
def _run_cmpgen_namelists(test_dir):
    ###############################################################################
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{get_cime_root()}:{get_tools_path()}"
    cmdstat, output, _ = run_cmd(
        "./case.cmpgen_namelists",
        combine_output=True,
        from_dir=test_dir,
        env=env,
    )
    return cmdstat, output


###############################################################################
class TestScheduler(object):
    ###############################################################################

    ###########################################################################
    def __init__(
        self,
        test_names,
        test_data=None,
        no_run=False,
        no_build=False,
        no_setup=False,
        no_batch=None,
        test_root=None,
        test_id=None,
        machine_name=None,
        compiler=None,
        baseline_root=None,
        baseline_cmp_name=None,
        baseline_gen_name=None,
        clean=False,
        namelists_only=False,
        project=None,
        parallel_jobs=None,
        walltime=None,
        proc_pool=None,
        use_existing=False,
        save_timing=False,
        queue=None,
        allow_baseline_overwrite=False,
        skip_tests_with_existing_baselines=False,
        output_root=None,
        force_procs=None,
        force_threads=None,
        mpilib=None,
        input_dir=None,
        pesfile=None,
        run_count=0,
        mail_user=None,
        mail_type=None,
        allow_pnl=False,
        non_local=False,
        single_exe=False,
        workflow=None,
        chksum=False,
        force_rebuild=False,
        driver=None,
    ):
        ###########################################################################
        self._cime_root = get_cime_root()
        self._cime_model = get_model()
        self._cime_driver = driver if driver is not None else get_cime_default_driver()
        self._save_timing = save_timing
        self._queue = queue
        self._test_data = (
            {} if test_data is None else test_data
        )  # Format:  {test_name -> {data_name -> data}}
        self._mpilib = mpilib  # allow override of default mpilib
        self._completed_tests = 0
        self._input_dir = input_dir
        self._pesfile = pesfile
        self._allow_baseline_overwrite = allow_baseline_overwrite
        self._skip_tests_with_existing_baselines = skip_tests_with_existing_baselines
        self._single_exe = single_exe
        if self._single_exe:
            self._allow_pnl = True
        else:
            self._allow_pnl = allow_pnl
        self._non_local = non_local
        self._build_groups = []
        self._workflow = workflow

        self._mail_user = mail_user
        self._mail_type = mail_type

        self._machobj = Machines(machine=machine_name)

        self._config = Config.instance()

        if self._config.calculate_mode_build_cost:
            # Current build system is unlikely to be able to productively use more than 16 cores
            self._model_build_cost = min(
                16, int((self._machobj.get_value("GMAKE_J") * 2) / 3) + 1
            )
        else:
            self._model_build_cost = 4

        # If user is forcing procs or threads, re-write test names to reflect this.
        if force_procs or force_threads:
            test_names = _translate_test_names_for_new_pecount(
                test_names, force_procs, force_threads
            )

        self._no_setup = no_setup
        self._no_build = no_build or no_setup or namelists_only
        self._no_run = no_run or self._no_build
        self._output_root = output_root
        # Figure out what project to use
        if project is None:
            self._project = get_project(machobj=self._machobj)
        else:
            self._project = project

        # We will not use batch system if user asked for no_batch or if current
        # machine is not a batch machine
        self._no_batch = no_batch or not self._machobj.has_batch_system()
        expect(
            not (self._no_batch and self._queue is not None),
            "Does not make sense to request a queue without batch system",
        )

        # Determine and resolve test_root
        if test_root is not None:
            self._test_root = test_root
        elif self._output_root is not None:
            self._test_root = self._output_root
        else:
            self._test_root = self._machobj.get_value("CIME_OUTPUT_ROOT")

        if self._project is not None:
            self._test_root = self._test_root.replace("$PROJECT", self._project)

        self._test_root = os.path.abspath(self._test_root)
        self._test_id = test_id if test_id is not None else get_timestamp()

        self._compiler = (
            self._machobj.get_default_compiler() if compiler is None else compiler
        )

        self._clean = clean

        self._namelists_only = namelists_only

        self._walltime = walltime

        if parallel_jobs is None:
            mach_parallel_jobs = self._machobj.get_value("NTEST_PARALLEL_JOBS")
            if mach_parallel_jobs is None:
                mach_parallel_jobs = self._machobj.get_value("MAX_MPITASKS_PER_NODE")
            self._parallel_jobs = min(len(test_names), mach_parallel_jobs)
        else:
            self._parallel_jobs = parallel_jobs

        logger.info(
            "create_test will do up to {} tasks simultaneously".format(
                self._parallel_jobs
            )
        )

        self._baseline_cmp_name = (
            baseline_cmp_name  # Implies comparison should be done if not None
        )
        self._baseline_gen_name = (
            baseline_gen_name  # Implies generation should be done if not None
        )

        # Compute baseline_root. Need to set some properties on machobj in order for
        # the baseline_root to resolve correctly.
        self._machobj.set_value("COMPILER", self._compiler)
        self._machobj.set_value("PROJECT", self._project)
        self._baseline_root = (
            os.path.abspath(baseline_root)
            if baseline_root is not None
            else self._machobj.get_value("BASELINE_ROOT")
        )

        if baseline_cmp_name or baseline_gen_name:
            if self._baseline_cmp_name:
                full_baseline_dir = os.path.join(
                    self._baseline_root, self._baseline_cmp_name
                )
                expect(
                    os.path.isdir(full_baseline_dir),
                    "Missing baseline comparison directory {}".format(
                        full_baseline_dir
                    ),
                )

            # the following is to assure that the existing generate directory is not overwritten
            if self._baseline_gen_name:
                full_baseline_dir = os.path.join(
                    self._baseline_root, self._baseline_gen_name
                )
                existing_baselines = []
                if skip_tests_with_existing_baselines:
                    tests_to_skip = []
                for test_name in test_names:
                    test_baseline = os.path.join(full_baseline_dir, test_name)
                    if os.path.isdir(test_baseline):
                        existing_baselines.append(test_baseline)
                        if allow_baseline_overwrite and run_count == 0:
                            if self._namelists_only:
                                clear_folder(os.path.join(test_baseline, "CaseDocs"))
                            else:
                                clear_folder(test_baseline)
                        elif skip_tests_with_existing_baselines:
                            tests_to_skip.append(test_name)
                expect(
                    allow_baseline_overwrite
                    or len(existing_baselines) == 0
                    or skip_tests_with_existing_baselines,
                    "Baseline directories already exists {}\n"
                    "Use -o or --skip-tests-with-existing-baselines to avoid this error".format(
                        existing_baselines
                    ),
                )
                if skip_tests_with_existing_baselines:
                    test_names = [
                        test for test in test_names if test not in tests_to_skip
                    ]

        if self._config.sort_tests:
            _order_tests_by_runtime(test_names, self._baseline_root)

        # This is the only data that multiple threads will simultaneously access
        # Each test has it's own value and setting/retrieving items from a dict
        # is atomic, so this should be fine to use without mutex.
        # name -> (phase, status)
        self._tests = OrderedDict()
        for test_name in test_names:
            self._tests[test_name] = (TEST_START, TEST_PASS_STATUS)

        # Oversubscribe by 1/4
        if proc_pool is None:
            pes = int(self._machobj.get_value("MAX_TASKS_PER_NODE"))
            self._proc_pool = int(pes * 1.25)
        else:
            self._proc_pool = int(proc_pool)

        logger.info(
            "create_test will use up to {} cores simultaneously".format(self._proc_pool)
        )

        self._procs_avail = self._proc_pool

        # Setup phases
        self._phases = list(PHASES)
        if self._no_setup:
            self._phases.remove(SETUP_PHASE)
        if self._no_build:
            self._phases.remove(SHAREDLIB_BUILD_PHASE)
            self._phases.remove(MODEL_BUILD_PHASE)
        if self._no_run:
            self._phases.remove(RUN_PHASE)

        if use_existing:
            for test in self._tests:
                with TestStatus(self._get_test_dir(test)) as ts:
                    if force_rebuild:
                        ts.set_status(SHAREDLIB_BUILD_PHASE, TEST_PEND_STATUS)

                    for phase, status in ts:
                        if phase in CORE_PHASES:
                            if status in [TEST_PEND_STATUS, TEST_FAIL_STATUS]:
                                if status == TEST_FAIL_STATUS:
                                    # Import for potential subsequent waits
                                    ts.set_status(
                                        phase, TEST_PEND_STATUS, TEST_RERUN_COMMENT
                                    )

                                # We need to pick up here
                                break

                            else:
                                if phase != SUBMIT_PHASE:
                                    # Somewhat subtle. Create_test considers submit/run to be the run phase,
                                    # so don't try to update test status for a passed submit phase
                                    self._update_test_status(
                                        test, phase, TEST_PEND_STATUS
                                    )
                                    self._update_test_status(test, phase, status)

                                    if phase == RUN_PHASE:
                                        logger.info(
                                            "Test {} passed and will not be re-run".format(
                                                test
                                            )
                                        )

                logger.info(
                    "Using existing test directory {}".format(self._get_test_dir(test))
                )
        else:
            # None of the test directories should already exist.
            for test in self._tests:
                expect(
                    not os.path.exists(self._get_test_dir(test)),
                    "Cannot create new case in directory '{}', it already exists."
                    " Pick a different test-id".format(self._get_test_dir(test)),
                )
                logger.info(
                    "Creating test directory {}".format(self._get_test_dir(test))
                )

        # Setup build groups
        if single_exe:
            self._build_groups = [tuple(self._tests.keys())]
        elif self._config.share_exes:
            # Any test that's in a shared-enabled suite with other tests should share exes
            self._build_groups = get_build_groups(self._tests)
        else:
            self._build_groups = [(item,) for item in self._tests]

        # Build group to exeroot map
        self._build_group_exeroots = {}
        for build_group in self._build_groups:
            self._build_group_exeroots[build_group] = None

        logger.debug("Build groups are:")
        for build_group in self._build_groups:
            for test_name in build_group:
                logger.debug(
                    "{}{}".format(
                        "  " if test_name == build_group[0] else "    ", test_name
                    )
                )

        self._chksum = chksum
        # By the end of this constructor, this program should never hard abort,
        # instead, errors will be placed in the TestStatus files for the various
        # tests cases

    ###########################################################################
    def get_testnames(self):
        ###########################################################################
        return list(self._tests.keys())

    ###########################################################################
    def _log_output(self, test, output):
        ###########################################################################
        test_dir = self._get_test_dir(test)
        if not os.path.isdir(test_dir):
            # Note: making this directory could cause create_newcase to fail
            # if this is run before.
            os.makedirs(test_dir)
        append_testlog(output, caseroot=test_dir)

    ###########################################################################
    def _get_case_id(self, test):
        ###########################################################################
        baseline_action_code = ""
        if self._baseline_gen_name:
            baseline_action_code += "G"
        if self._baseline_cmp_name:
            baseline_action_code += "C"
        if len(baseline_action_code) > 0:
            return "{}.{}.{}".format(test, baseline_action_code, self._test_id)
        else:
            return "{}.{}".format(test, self._test_id)

    ###########################################################################
    def _get_test_dir(self, test):
        ###########################################################################
        return os.path.join(self._test_root, self._get_case_id(test))

    ###########################################################################
    def _get_test_data(self, test):
        ###########################################################################
        # Must be atomic
        return self._tests[test]

    ###########################################################################
    def _is_broken(self, test):
        ###########################################################################
        status = self._get_test_status(test)
        return status != TEST_PASS_STATUS and status != TEST_PEND_STATUS

    ###########################################################################
    def _work_remains(self, test):
        ###########################################################################
        test_phase, test_status = self._get_test_data(test)
        return (
            test_status == TEST_PASS_STATUS or test_status == TEST_PEND_STATUS
        ) and test_phase != self._phases[-1]

    ###########################################################################
    def _get_test_status(self, test, phase=None):
        ###########################################################################
        curr_phase, curr_status = self._get_test_data(test)
        if phase is None or phase == curr_phase:
            return curr_status
        else:
            # Assume all future phases are PEND
            if phase is not None and self._phases.index(phase) > self._phases.index(
                curr_phase
            ):
                return TEST_PEND_STATUS

            # Assume all older phases PASSed
            return TEST_PASS_STATUS

    ###########################################################################
    def _get_test_phase(self, test):
        ###########################################################################
        return self._get_test_data(test)[0]

    ###########################################################################
    def _update_test_status(self, test, phase, status):
        ###########################################################################
        phase_idx = self._phases.index(phase)
        old_phase, old_status = self._get_test_data(test)

        if old_phase == phase:
            expect(
                old_status == TEST_PEND_STATUS,
                "Only valid to transition from PEND to something else, found '{}' for phase '{}'".format(
                    old_status, phase
                ),
            )
            expect(status != TEST_PEND_STATUS, "Cannot transition from PEND -> PEND")
        else:
            expect(
                old_status == TEST_PASS_STATUS,
                "Why did we move on to next phase when prior phase did not pass?",
            )
            expect(
                status == TEST_PEND_STATUS, "New phase should be set to pending status"
            )
            expect(
                self._phases.index(old_phase) == phase_idx - 1,
                "Skipped phase? {} {}".format(old_phase, phase_idx),
            )

        # Must be atomic
        self._tests[test] = (phase, status)

    ###########################################################################
    def _shell_cmd_for_phase(self, test, cmd, phase, from_dir=None):
        ###########################################################################
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{get_cime_root()}:{get_tools_path()}"

        while True:
            rc, output, errput = run_cmd(cmd, from_dir=from_dir, env=env)
            if rc != 0:
                self._log_output(
                    test,
                    "{} FAILED for test '{}'.\nCommand: {}\nOutput: {}\n".format(
                        phase, test, cmd, output + "\n" + errput
                    ),
                )
                # Temporary hack to get around odd file descriptor use by
                # buildnml scripts.
                if "bad interpreter" in output:
                    time.sleep(1)
                    continue
                else:
                    return False, errput
            else:
                # We don't want "RUN PASSED" in the TestStatus.log if the only thing that
                # succeeded was the submission.
                phase = "SUBMIT" if phase == RUN_PHASE else phase
                self._log_output(
                    test,
                    "{} PASSED for test '{}'.\nCommand: {}\nOutput: {}\n".format(
                        phase, test, cmd, output + "\n" + errput
                    ),
                )
                return True, errput

    ###########################################################################
    def _create_newcase_phase(self, test):
        ###########################################################################
        test_dir = self._get_test_dir(test)

        _, case_opts, grid, compset, machine, compiler, test_mods = parse_test_name(
            test
        )

        os.environ["FROM_CREATE_TEST"] = "True"
        create_newcase_cmd = "{} {} --case {} --res {} --compset {} --test".format(
            sys.executable,
            os.path.join(self._cime_root, "CIME", "scripts", "create_newcase.py"),
            test_dir,
            grid,
            compset,
        )

        if machine is not None:
            create_newcase_cmd += " --machine {}".format(machine)
        if compiler is not None:
            create_newcase_cmd += " --compiler {}".format(compiler)
        if self._project is not None:
            create_newcase_cmd += " --project {} ".format(self._project)
        if self._output_root is not None:
            create_newcase_cmd += " --output-root {} ".format(self._output_root)
        if self._input_dir is not None:
            create_newcase_cmd += " --input-dir {} ".format(self._input_dir)
        if self._non_local:
            create_newcase_cmd += " --non-local"
        if self._workflow:
            create_newcase_cmd += " --workflow {}".format(self._workflow)
        if self._pesfile is not None:
            create_newcase_cmd += " --pesfile {} ".format(self._pesfile)

        create_newcase_cmd += f" --srcroot {get_src_root()}"

        mpilib = None
        ninst = 1
        ncpl = 1
        driver = self._cime_driver
        if case_opts is not None:
            for case_opt in case_opts:  # pylint: disable=not-an-iterable
                if case_opt.startswith("M"):
                    mpilib = case_opt[1:]
                    create_newcase_cmd += " --mpilib {}".format(mpilib)
                    logger.debug(" MPILIB set to {}".format(mpilib))
                elif case_opt.startswith("N"):
                    expect(ncpl == 1, "Cannot combine _C and _N options")
                    ninst = case_opt[1:]
                    create_newcase_cmd += " --ninst {}".format(ninst)
                    logger.debug(" NINST set to {}".format(ninst))
                elif case_opt.startswith("C"):
                    expect(ninst == 1, "Cannot combine _C and _N options")
                    ncpl = case_opt[1:]
                    create_newcase_cmd += " --ninst {} --multi-driver".format(ncpl)
                    logger.debug(" NCPL set to {}".format(ncpl))
                elif case_opt.startswith("P"):
                    pesize = case_opt[1:]
                    create_newcase_cmd += " --pecount {}".format(pesize)
                elif case_opt.startswith("V"):
                    driver = case_opt[1:]

        create_newcase_cmd += " --driver {}".format(driver)

        if (
            "--ninst" in create_newcase_cmd
            and not "--multi-driver" in create_newcase_cmd
        ):
            if "--driver nuopc" in create_newcase_cmd or (
                "--driver" not in create_newcase_cmd and driver == "nuopc"
            ):
                expect(False, "_N option not supported by nuopc driver, use _C instead")

        if test_mods is not None:
            create_newcase_cmd += " --user-mods-dir "

            try:
                test_mods_paths = find_test_mods(self._cime_driver, test_mods)
            except CIMEError as e:
                error = f"{e}"

                self._log_output(test, error)

                return False, error
            else:
                test_mods_paths = " ".join(test_mods_paths)

                create_newcase_cmd += f"{test_mods_paths}"

        # create_test mpilib option overrides default but not explicitly set case_opt mpilib
        if mpilib is None and self._mpilib is not None:
            create_newcase_cmd += " --mpilib {}".format(self._mpilib)
            logger.debug(" MPILIB set to {}".format(self._mpilib))

        if self._queue is not None:
            create_newcase_cmd += " --queue={}".format(self._queue)
        else:
            # We need to hard code the queue for this test on cheyenne
            # otherwise it runs in share and fails intermittently
            test_case = parse_test_name(test)[0]
            if test_case == "NODEFAIL":
                machine = (
                    machine if machine is not None else self._machobj.get_machine_name()
                )
                if machine == "cheyenne":
                    create_newcase_cmd += " --queue=regular"

        if self._walltime is not None:
            create_newcase_cmd += " --walltime {}".format(self._walltime)
        else:
            # model specific ways of setting time
            if self._config.sort_tests:
                recommended_time = _get_time_est(test, self._baseline_root)

                if recommended_time is not None:
                    create_newcase_cmd += " --walltime {}".format(recommended_time)

            else:
                if (
                    test in self._test_data
                    and "options" in self._test_data[test]
                    and "wallclock" in self._test_data[test]["options"]
                ):
                    create_newcase_cmd += " --walltime {}".format(
                        self._test_data[test]["options"]["wallclock"]
                    )
        if (
            test in self._test_data
            and "options" in self._test_data[test]
            and "workflow" in self._test_data[test]["options"]
        ):
            create_newcase_cmd += " --workflow {}".format(
                self._test_data[test]["options"]["workflow"]
            )

        logger.debug("Calling create_newcase: " + create_newcase_cmd)
        return self._shell_cmd_for_phase(test, create_newcase_cmd, CREATE_NEWCASE_PHASE)

    ###########################################################################
    def _xml_phase(self, test):
        ###########################################################################
        test_case, case_opts, _, _, _, compiler, _ = parse_test_name(test)

        # Create, fill and write an envtest object
        test_dir = self._get_test_dir(test)
        envtest = EnvTest(test_dir)

        # Find driver. It may be different for the current test if V testopt is used
        driver = self._cime_driver
        if case_opts is not None:
            for case_opt in case_opts:  # pylint: disable=not-an-iterable
                if case_opt.startswith("V"):
                    driver = case_opt[1:]

        # Determine list of component classes that this coupler/driver knows how
        # to deal with. This list follows the same order as compset longnames follow.
        files = Files(comp_interface=driver)
        ufs_driver = os.environ.get("UFS_DRIVER")
        attribute = None
        if ufs_driver:
            attribute = {"component": ufs_driver}

        drv_config_file = files.get_value("CONFIG_CPL_FILE", attribute=attribute)

        if driver == "nuopc" and not os.path.exists(drv_config_file):
            drv_config_file = files.get_value("CONFIG_CPL_FILE", {"component": "cpl"})
        expect(
            os.path.exists(drv_config_file),
            "File {} not found, cime driver {}".format(drv_config_file, driver),
        )

        drv_comp = Component(drv_config_file, "CPL")

        envtest.add_elements_by_group(files, {}, "env_test.xml")
        envtest.add_elements_by_group(drv_comp, {}, "env_test.xml")
        envtest.set_value("TESTCASE", test_case)
        envtest.set_value("TEST_TESTID", self._test_id)
        envtest.set_value("CASEBASEID", test)
        memleak_tolerance = self._machobj.get_value(
            "TEST_MEMLEAK_TOLERANCE", resolved=False
        )
        if (
            test in self._test_data
            and "options" in self._test_data[test]
            and "memleak_tolerance" in self._test_data[test]["options"]
        ):
            memleak_tolerance = self._test_data[test]["options"]["memleak_tolerance"]

        envtest.set_value(
            "TEST_MEMLEAK_TOLERANCE",
            0.10 if memleak_tolerance is None else memleak_tolerance,
        )

        test_argv = "-testname {} -testroot {}".format(test, self._test_root)
        if self._baseline_gen_name:
            test_argv += " -generate {}".format(self._baseline_gen_name)
            basegen_case_fullpath = os.path.join(
                self._baseline_root, self._baseline_gen_name, test
            )
            logger.debug("basegen_case is {}".format(basegen_case_fullpath))
            envtest.set_value("BASELINE_NAME_GEN", self._baseline_gen_name)
            envtest.set_value(
                "BASEGEN_CASE", os.path.join(self._baseline_gen_name, test)
            )
        if self._baseline_cmp_name:
            test_argv += " -compare {}".format(self._baseline_cmp_name)
            envtest.set_value("BASELINE_NAME_CMP", self._baseline_cmp_name)
            envtest.set_value(
                "BASECMP_CASE", os.path.join(self._baseline_cmp_name, test)
            )

        envtest.set_value("TEST_ARGV", test_argv)
        envtest.set_value("CLEANUP", self._clean)

        envtest.set_value("BASELINE_ROOT", self._baseline_root)
        envtest.set_value("GENERATE_BASELINE", self._baseline_gen_name is not None)
        envtest.set_value("COMPARE_BASELINE", self._baseline_cmp_name is not None)
        envtest.set_value(
            "CCSM_CPRNC", self._machobj.get_value("CCSM_CPRNC", resolved=False)
        )
        tput_tolerance = self._machobj.get_value("TEST_TPUT_TOLERANCE", resolved=False)
        if (
            test in self._test_data
            and "options" in self._test_data[test]
            and "tput_tolerance" in self._test_data[test]["options"]
        ):
            tput_tolerance = self._test_data[test]["options"]["tput_tolerance"]

        envtest.set_value(
            "TEST_TPUT_TOLERANCE", 0.25 if tput_tolerance is None else tput_tolerance
        )

        # Add the test instructions from config_test to env_test in the case
        config_test = Tests()
        testnode = config_test.get_test_node(test_case)
        envtest.add_test(testnode)

        if compiler == "nag":
            envtest.set_value("FORCE_BUILD_SMP", "FALSE")

        # Determine case_opts from the test_case
        if case_opts is not None:
            logger.debug("case_opts are {} ".format(case_opts))
            for opt in case_opts:  # pylint: disable=not-an-iterable

                logger.debug("case_opt is {}".format(opt))
                if opt == "D":
                    envtest.set_test_parameter("DEBUG", "TRUE")
                    logger.debug(" DEBUG set to TRUE")

                elif opt == "E":
                    envtest.set_test_parameter("USE_ESMF_LIB", "TRUE")
                    logger.debug(" USE_ESMF_LIB set to TRUE")

                elif opt == "CG":
                    envtest.set_test_parameter("CALENDAR", "GREGORIAN")
                    logger.debug(" CALENDAR set to {}".format(opt))

                elif opt.startswith("L"):
                    match = re.match("L([A-Za-z])([0-9]*)", opt)
                    stop_option = {
                        "y": "nyears",
                        "m": "nmonths",
                        "d": "ndays",
                        "h": "nhours",
                        "s": "nseconds",
                        "n": "nsteps",
                    }
                    opt = match.group(1)
                    envtest.set_test_parameter("STOP_OPTION", stop_option[opt])
                    opti = match.group(2)
                    envtest.set_test_parameter("STOP_N", opti)

                    logger.debug(" STOP_OPTION set to {}".format(stop_option[opt]))
                    logger.debug(" STOP_N      set to {}".format(opti))

                elif opt.startswith("R"):
                    # R option is for testing in PTS_MODE or Single Column Model
                    #  (SCM) mode
                    envtest.set_test_parameter("PTS_MODE", "TRUE")

                    # For PTS_MODE, set all tasks and threads to 1
                    comps = ["ATM", "LND", "ICE", "OCN", "CPL", "GLC", "ROF", "WAV"]

                    for comp in comps:
                        envtest.set_test_parameter("NTASKS_" + comp, "1")
                        envtest.set_test_parameter("NTHRDS_" + comp, "1")
                        envtest.set_test_parameter("ROOTPE_" + comp, "0")
                        envtest.set_test_parameter("PIO_TYPENAME", "netcdf")

                elif opt.startswith("A"):
                    # A option is for testing in ASYNC IO mode, only available with nuopc driver and pio2
                    envtest.set_test_parameter("PIO_ASYNC_INTERFACE", "TRUE")
                    expect(
                        driver == "nuopc", "ASYNC IO mode only works with nuopc driver"
                    )
                    envtest.set_test_parameter("PIO_VERSION", "2")
                    match = re.match("A([0-9]+)x?([0-9])*", opt)
                    envtest.set_test_parameter("PIO_NUMTASKS_CPL", match.group(1))
                    if match.group(2):
                        envtest.set_test_parameter("PIO_STRIDE_CPL", match.group(2))

                elif (
                    opt.startswith("I")
                    or opt.startswith(  # Marker to distinguish tests with same name - ignored
                        "M"
                    )
                    or opt.startswith("P")  # handled in create_newcase
                    or opt.startswith("N")  # handled in create_newcase
                    or opt.startswith("C")  # handled in create_newcase
                    or opt.startswith("V")  # handled in create_newcase
                    or opt.startswith("G")  # handled in create_newcase
                    or opt == "B"  # handled in create_newcase
                ):  # handled in run_phase
                    pass

                elif opt.startswith("IOP"):
                    logger.warning("IOP test option not yet implemented")
                else:
                    expect(False, "Could not parse option '{}' ".format(opt))

        envtest.write()
        lock_file("env_run.xml", caseroot=test_dir, newname="env_run.orig.xml")

        with Case(test_dir, read_only=False, non_local=self._non_local) as case:
            if self._output_root is None:
                self._output_root = case.get_value("CIME_OUTPUT_ROOT")
            # if we are running a single test we don't need sharedlibroot
            if len(self._tests) > 1 and self._config.common_sharedlibroot:
                case.set_value(
                    "SHAREDLIBROOT",
                    os.path.join(
                        self._output_root, "sharedlibroot.{}".format(self._test_id)
                    ),
                )
            envtest.set_initial_values(case)
            case.set_value("TEST", True)
            if is_perf_test(test):
                case.set_value("SAVE_TIMING", True)
            else:
                case.set_value("SAVE_TIMING", self._save_timing)

            # handle single-exe here, all cases will use the EXEROOT from
            # the first case in the build group
            is_first_test, _, my_build_group = self._get_build_group(test)
            if is_first_test:
                expect(
                    self._build_group_exeroots[my_build_group] is None,
                    "Should not already have exeroot",
                )
                self._build_group_exeroots[my_build_group] = case.get_value("EXEROOT")
            else:
                build_group_exeroot = self._build_group_exeroots[my_build_group]
                expect(build_group_exeroot is not None, "Should already have exeroot")
                case.set_value("EXEROOT", build_group_exeroot)

            # Scale back build parallelism on systems with few cores
            if self._model_build_cost > self._proc_pool:
                case.set_value("GMAKE_J", self._proc_pool)
                self._model_build_cost = self._proc_pool

        return True, ""

    ###########################################################################
    def _setup_phase(self, test):
        ###########################################################################
        test_dir = self._get_test_dir(test)
        rv = self._shell_cmd_for_phase(
            test, "./case.setup", SETUP_PHASE, from_dir=test_dir
        )

        # cmpgen_namelists is called again with checks later in _setup_phase(). This call is
        # necessary for the correct behavior of --skip-tests-with-existing-baselines, and we don't
        # need to check it for errors.
        if rv[0]:
            _run_cmpgen_namelists(test_dir)

        if self._single_exe:
            with Case(self._get_test_dir(test), read_only=False) as case:
                tests = Tests()

                try:
                    tests.support_single_exe(case)
                except Exception:
                    self._update_test_status_file(test, SETUP_PHASE, TEST_FAIL_STATUS)

                    raise

        return rv

    ###########################################################################
    def _sharedlib_build_phase(self, test):
        ###########################################################################
        is_first_test, first_test, _ = self._get_build_group(test)
        if not is_first_test:
            if (
                self._get_test_status(first_test, phase=SHAREDLIB_BUILD_PHASE)
                == TEST_PASS_STATUS
            ):
                return True, ""
            else:
                return False, "Cannot use build for test {} because it failed".format(
                    first_test
                )

        test_dir = self._get_test_dir(test)
        result = self._shell_cmd_for_phase(
            test,
            "./case.build --sharedlib-only",
            SHAREDLIB_BUILD_PHASE,
            from_dir=test_dir,
        )

        # It's OK for this command to fail with baseline diffs but not catastrophically
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{get_cime_root()}:{get_tools_path()}"
        cmdstat, output = _run_cmpgen_namelists(test_dir)
        try:
            expect(
                cmdstat in [0, TESTS_FAILED_ERR_CODE],
                "Fatal error in case.cmpgen_namelists: {}".format(output),
            )
        except Exception:
            self._update_test_status_file(test, SETUP_PHASE, TEST_FAIL_STATUS)
            raise

        return result

    ###########################################################################
    def _get_build_group(self, test):
        ###########################################################################
        for build_group in self._build_groups:
            if test in build_group:
                return test == build_group[0], build_group[0], build_group

        expect(False, "No build group for test '{}'".format(test))

    ###########################################################################
    def _model_build_phase(self, test):
        ###########################################################################
        is_first_test, first_test, _ = self._get_build_group(test)

        test_dir = self._get_test_dir(test)

        if not is_first_test:
            if (
                self._get_test_status(first_test, phase=MODEL_BUILD_PHASE)
                == TEST_PASS_STATUS
            ):
                with Case(test_dir, read_only=False) as case:
                    post_build(
                        case, [], build_complete=True, save_build_provenance=False
                    )

                return True, ""
            else:
                return False, "Cannot use build for test {} because it failed".format(
                    first_test
                )

        return self._shell_cmd_for_phase(
            test, "./case.build --model-only", MODEL_BUILD_PHASE, from_dir=test_dir
        )

    ###########################################################################
    def _run_phase(self, test):
        ###########################################################################
        test_dir = self._get_test_dir(test)

        case_opts = parse_test_name(test)[1]
        if (
            case_opts is not None
            and "B" in case_opts  # pylint: disable=unsupported-membership-test
        ):
            self._log_output(test, "{} SKIPPED for test '{}'".format(RUN_PHASE, test))
            self._update_test_status_file(test, SUBMIT_PHASE, TEST_PASS_STATUS)
            self._update_test_status_file(test, RUN_PHASE, TEST_PASS_STATUS)

            return True, "SKIPPED"
        else:
            cmd = "./case.submit"
            if not self._allow_pnl:
                cmd += " --skip-preview-namelist"
            if self._no_batch:
                cmd += " --no-batch"
            if self._mail_user:
                cmd += " --mail-user={}".format(self._mail_user)
            if self._mail_type:
                cmd += " -M={}".format(",".join(self._mail_type))
            if self._chksum:
                cmd += " --chksum"

            return self._shell_cmd_for_phase(test, cmd, RUN_PHASE, from_dir=test_dir)

    ###########################################################################
    def _run_catch_exceptions(self, test, phase, run):
        ###########################################################################
        try:
            return run(test)
        except Exception as e:
            exc_tb = sys.exc_info()[2]
            errput = "Test '{}' failed in phase '{}' with exception '{}'\n".format(
                test, phase, str(e)
            )
            errput += "".join(traceback.format_tb(exc_tb))
            self._log_output(test, errput)
            return False, errput

    ###########################################################################
    def _get_procs_needed(self, test, phase, threads_in_flight=None, no_batch=False):
        ###########################################################################
        """
        Return the number of processors/cores needed to run phase of test.

        Returns None if the phase of this test is currently ineligible to run.
        """
        # For build pools, we must wait for the first case to complete XML, SHAREDLIB,
        # and MODEL_BUILD phases before the other cases can do those phases
        is_first_test, first_test, _ = self._get_build_group(test)

        if not is_first_test:
            build_group_dep_phases = [
                XML_PHASE,
                SHAREDLIB_BUILD_PHASE,
                MODEL_BUILD_PHASE,
            ]
            if phase in build_group_dep_phases:
                if self._get_test_status(first_test, phase=phase) == TEST_PEND_STATUS:
                    return None  # None indicates job is ineligible to run
                else:
                    return 1

        if phase == RUN_PHASE and (self._no_batch or no_batch):
            test_dir = self._get_test_dir(test)
            total_pes = EnvMachPes(test_dir, read_only=True).get_value("TOTALPES")
            return total_pes

        elif phase == SHAREDLIB_BUILD_PHASE:
            if self._config.serialize_sharedlib_builds:
                # Will force serialization of sharedlib builds
                # TODO - instead of serializing, compute all library configs needed and build
                # them all in parallel
                for _, _, running_phase in threads_in_flight.values():
                    if running_phase == SHAREDLIB_BUILD_PHASE:
                        return None

            return 1
        elif phase == MODEL_BUILD_PHASE:
            # Model builds now happen in parallel
            return self._model_build_cost
        else:
            return 1

    ###########################################################################
    def _wait_for_something_to_finish(self, threads_in_flight):
        ###########################################################################
        expect(len(threads_in_flight) <= self._parallel_jobs, "Oversubscribed?")
        finished_tests = []
        while not finished_tests:
            for test, thread_info in threads_in_flight.items():
                if not thread_info[0].is_alive():
                    finished_tests.append((test, thread_info[1]))

            if not finished_tests:
                time.sleep(0.2)

        for finished_test, procs_needed in finished_tests:
            self._procs_avail += procs_needed
            del threads_in_flight[finished_test]

    ###########################################################################
    def _update_test_status_file(self, test, test_phase, status):
        ###########################################################################
        """
        In general, test_scheduler should not be responsible for updating
        the TestStatus file, but there are a few cases where it has to.
        """
        test_dir = self._get_test_dir(test)
        with TestStatus(test_dir=test_dir, test_name=test) as ts:
            ts.set_status(test_phase, status)

    ###########################################################################
    def _consumer(self, test, test_phase, phase_method):
        ###########################################################################
        before_time = time.time()
        success, errors = self._run_catch_exceptions(test, test_phase, phase_method)
        elapsed_time = time.time() - before_time
        status = (
            (
                TEST_PEND_STATUS
                if test_phase == RUN_PHASE and not self._no_batch
                else TEST_PASS_STATUS
            )
            if success
            else TEST_FAIL_STATUS
        )

        if status != TEST_PEND_STATUS:
            self._update_test_status(test, test_phase, status)

        if not self._work_remains(test):
            self._completed_tests += 1
            total = len(self._tests)
            status_str = "Finished {} for test {} in {:f} seconds ({}). [COMPLETED {:d} of {:d}]".format(
                test_phase, test, elapsed_time, status, self._completed_tests, total
            )
        else:
            status_str = "Finished {} for test {} in {:f} seconds ({})".format(
                test_phase, test, elapsed_time, status
            )

        if not success:
            status_str += "\n    Case dir: {}\n".format(self._get_test_dir(test))
            status_str += "    Errors were:\n        {}\n".format(
                "\n        ".join(errors.splitlines())
            )

        logger.info(status_str)

        is_first_test = self._get_build_group(test)[0]

        if test_phase in [CREATE_NEWCASE_PHASE, XML_PHASE] or (
            not is_first_test
            and test_phase in [SHAREDLIB_BUILD_PHASE, MODEL_BUILD_PHASE]
        ):
            # These are the phases for which TestScheduler is reponsible for
            # updating the TestStatus file
            self._update_test_status_file(test, test_phase, status)

        if test_phase == XML_PHASE:
            append_status(
                "Case Created using: " + " ".join(sys.argv),
                "README.case",
                caseroot=self._get_test_dir(test),
            )

        # On batch systems, we want to immediately submit to the queue, because
        # it's very cheap to submit and will get us a better spot in line
        if (
            success
            and not self._no_run
            and not self._no_batch
            and test_phase == MODEL_BUILD_PHASE
        ):
            logger.info(
                "Starting {} for test {} with 1 proc on interactive node and {:d} procs on compute nodes".format(
                    RUN_PHASE,
                    test,
                    self._get_procs_needed(test, RUN_PHASE, no_batch=True),
                )
            )
            self._update_test_status(test, RUN_PHASE, TEST_PEND_STATUS)
            self._consumer(test, RUN_PHASE, self._run_phase)

    ###########################################################################
    def _producer_indv_test_launch(self, test, threads_in_flight):
        ###########################################################################
        """
        Launch the next phase of test if possible. Return True if launched
        """
        test_phase, test_status = self._get_test_data(test)
        expect(test_status != TEST_PEND_STATUS, test)
        next_phase = self._phases[self._phases.index(test_phase) + 1]
        procs_needed = self._get_procs_needed(test, next_phase, threads_in_flight)

        if procs_needed is None:
            # This test cannot run now so skip
            return False

        elif procs_needed > self._proc_pool:
            # This test is asking for more than we can ever provide
            # This should only ever happen for RUN_PHASE
            msg = f"Test {test} phase {next_phase} requested more ({procs_needed}) than entire pool (self._proc_pool)"
            expect(next_phase == RUN_PHASE, msg)

            # CIME phase won't be run, so we need to update TEST_STATUS ourselves
            self._update_test_status_file(test, SUBMIT_PHASE, TEST_PASS_STATUS)
            self._update_test_status_file(test, RUN_PHASE, TEST_FAIL_STATUS)

            # Update our internal state that this test failed
            self._update_test_status(test, next_phase, TEST_PEND_STATUS)
            self._update_test_status(test, next_phase, TEST_FAIL_STATUS)

            logger.warning(msg)
            self._log_output(test, msg)

            # We did run the phase in some sense in that we instantly failed it
            return True

        elif procs_needed <= self._procs_avail:
            # We can run this test!
            self._procs_avail -= procs_needed

            # Necessary to print this way when multiple threads printing
            logger.info(
                f"Starting {next_phase} for test {test} with {procs_needed} procs"
            )

            self._update_test_status(test, next_phase, TEST_PEND_STATUS)
            phase_method = getattr(self, f"_{next_phase.lower()}_phase")
            new_thread = threading.Thread(
                target=self._consumer,
                args=(test, next_phase, phase_method),
            )
            threads_in_flight[test] = (new_thread, procs_needed, next_phase)
            new_thread.start()

            logger.debug("  Current workload:")
            total_procs = 0
            for the_test, the_data in threads_in_flight.items():
                logger.debug(f"    {the_test}: {the_data[2]} -> {the_data[1]}")
                total_procs += the_data[1]

            logger.debug(f"    Total procs in use: {total_procs}")

            return True

        else:
            # There aren't enough free procs to run this phase, so skip
            return False

    ###########################################################################
    def _producer(self):
        ###########################################################################
        threads_in_flight = {}  # test-name -> (thread, procs, phase)
        while True:
            work_to_do = False
            num_threads_launched_this_iteration = 0
            for test in self._tests:
                logger.debug("test_name: " + test)

                if self._work_remains(test):
                    work_to_do = True

                    # If we have no workers available, immediately break out of loop so we can wait
                    if len(threads_in_flight) == self._parallel_jobs:
                        break

                    # Check if this test is already running a phase. If so, we can't
                    # launch a new phase now.
                    if test not in threads_in_flight:
                        launched = self._producer_indv_test_launch(
                            test, threads_in_flight
                        )
                        if launched:
                            num_threads_launched_this_iteration += 1

            if not work_to_do:
                break

            if num_threads_launched_this_iteration == 0:
                # No free resources, wait for something in flight to finish
                self._wait_for_something_to_finish(threads_in_flight)

        for unfinished_thread, _, _ in threads_in_flight.values():
            unfinished_thread.join()

    ###########################################################################
    def _setup_cs_files(self):
        ###########################################################################
        try:
            template_path = get_template_path()

            create_cs_status(test_root=self._test_root, test_id=self._test_id)

            template_file = os.path.join(template_path, "cs.submit.template")
            template = open(template_file, "r").read()
            setup_cmd = "./case.setup" if self._no_setup else ":"
            build_cmd = "./case.build" if self._no_build else ":"
            test_cmd = "./case.submit"
            template = (
                template.replace("<SETUP_CMD>", setup_cmd)
                .replace("<BUILD_CMD>", build_cmd)
                .replace("<RUN_CMD>", test_cmd)
                .replace("<TESTID>", self._test_id)
            )

            if self._no_run:
                cs_submit_file = os.path.join(
                    self._test_root, "cs.submit.{}".format(self._test_id)
                )
                with open(cs_submit_file, "w") as fd:
                    fd.write(template)
                os.chmod(
                    cs_submit_file,
                    os.stat(cs_submit_file).st_mode | stat.S_IXUSR | stat.S_IXGRP,
                )

            if self._config.use_testreporter_template:
                template_file = os.path.join(template_path, "testreporter.template")
                template = open(template_file, "r").read()
                template = template.replace("<PATH>", get_tools_path())
                testreporter_file = os.path.join(self._test_root, "testreporter")
                with open(testreporter_file, "w") as fd:
                    fd.write(template)
                os.chmod(
                    testreporter_file,
                    os.stat(testreporter_file).st_mode | stat.S_IXUSR | stat.S_IXGRP,
                )

        except Exception as e:
            logger.warning("FAILED to set up cs files: {}".format(str(e)))

    ###########################################################################
    def run_tests(
        self,
        wait=False,
        check_throughput=False,
        check_memory=False,
        ignore_namelists=False,
        ignore_diffs=False,
        ignore_memleak=False,
    ):
        ###########################################################################
        """
        Main API for this class.

        Return True if all tests passed.
        """
        start_time = time.time()

        # Tell user what will be run
        logger.info("RUNNING TESTS:")
        for test in self._tests:
            logger.info("  {}".format(test))

        # Setup cs files
        self._setup_cs_files()

        GenericXML.DISABLE_CACHING = True
        self._producer()
        GenericXML.DISABLE_CACHING = False

        expect(threading.active_count() == 1, "Leftover threads?")

        config = Config.instance()

        # Copy TestStatus files to baselines for tests that have already failed.
        if config.baseline_store_teststatus:
            for test in self._tests:
                status = self._get_test_data(test)[1]
                if (
                    status not in [TEST_PASS_STATUS, TEST_PEND_STATUS]
                    and self._baseline_gen_name
                ):
                    basegen_case_fullpath = os.path.join(
                        self._baseline_root, self._baseline_gen_name, test
                    )
                    test_dir = self._get_test_dir(test)
                    generate_teststatus(test_dir, basegen_case_fullpath)

        no_need_to_wait = self._no_run or self._no_batch
        if no_need_to_wait:
            wait = False

        expect_test_complete = not self._no_run and (self._no_batch or wait)

        logger.info("Waiting for tests to finish")
        rv = wait_for_tests(
            glob.glob(
                os.path.join(self._test_root, "*{}/TestStatus".format(self._test_id))
            ),
            no_wait=not wait,
            check_throughput=check_throughput,
            check_memory=check_memory,
            ignore_namelists=ignore_namelists,
            ignore_diffs=ignore_diffs,
            ignore_memleak=ignore_memleak,
            no_run=self._no_run,
            expect_test_complete=expect_test_complete,
        )

        if not no_need_to_wait and not wait:
            logger.info(
                "Due to presence of batch system, create_test will exit before tests are complete.\n"
                "To force create_test to wait for full completion, use --wait"
            )

        logger.info("test-scheduler took {} seconds".format(time.time() - start_time))

        return rv
