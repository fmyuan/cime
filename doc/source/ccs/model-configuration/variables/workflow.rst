.. _model_config_workflow:

WORKFLOW_SPEC_FILE
==================

.. contents::
   :local:

Overview
--------
Provides CIME with a models various ``workflows``.

Entry
-----

This is an example entry for ``config_files.xml``.

.. code-block:: xml

   <entry id="WORKFLOW_SPEC_FILE">
      <type>char</type>
      <default_value>$SRCROOT/cime_config/machines/config_workflow.xml</default_value>
      <group>case_last</group>
      <file>env_case.xml</file>
      <desc>file containing workflow (for documentation only - DO NOT EDIT)</desc>
      <schema>$CIMEROOT/CIME/data/config/xml_schemas/config_workflow.xsd</schema>
   </entry>

Schema Definition
-----------------

======================= ==================================
Element/Attributes      Description
======================= ==================================
workflow_jobs           Describe jobs in a workflow.
id                      Workflow identifier.
prepend                 Workflow to prepend to current workflow.
append                  Workflow to append to current workflow.
job                     Describe the job.
name                    Name of the job.
template                Template file for job submission.
hidden                  
dependency              Job dependencies.
prereq                  Job pre-requirements.
runtime_parameters      Describe runtime parameters for the job.
MACH                    Which machine these runtime parameters should be used on.
task_count              Task count for the job.
tasks_per_node          Number of tasks per node.
walltime                Walltime for the job.
======================= ==================================

.. code-block:: xml

    <!-- Generated with doc/generate_xmlschema.py CIME/data/config/xml_schemas/config_workflow.xsd config_workflow on 2025-02-11 -->

    <!-- Attributes 'version' is required-->
    <!-- Occurences min: 1 max: 1-->
    <config_workflow version="">
        <!-- Attributes 'id' is required,'prepend' is optional,'append' is optional-->
        <!-- Occurences min: 1 max: Unlimited-->
        <workflow_jobs id="" prepend="" append="">
            <!-- Attributes 'name' is required-->
            <!-- Occurences min: 1 max: Unlimited-->
            <job name="">
                <!-- Occurences min: 1 max: 1-->
                <template></template>
                <!-- Occurences min: 0 max: 1-->
                <hidden></hidden>
                <!-- Occurences min: 0 max: 1-->
                <dependency></dependency>
                <!-- Occurences min: 1 max: 1-->
                <prereq></prereq>
                <!-- Attributes 'MACH' is optional-->
                <!-- Occurences min: 0 max: Unlimited-->
                <runtime_parameters MACH="">
                    <!-- Occurences min: 0 max: 1-->
                    <task_count></task_count>
                    <!-- Occurences min: 0 max: 1-->
                    <tasks_per_node></tasks_per_node>
                    <!-- Occurences min: 0 max: 1-->
                    <walltime></walltime>
                </runtime_parameters>
            </job>
        </workflow_jobs>
    </config_workflow>
