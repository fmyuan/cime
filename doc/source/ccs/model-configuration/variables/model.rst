.. _model_config_model:

MODEL
=====

.. contents::
    :local:

Overview
--------
This variable defines the name of the Model.

Entry
-----
The following is an example entry for ``MODEL`` in ``config_files.xml``.

Only a single value is required.

.. code-block:: xml
    
    <entry id="MODEL">
        <type>char</type>
        <default_value>e3sm</default_value>
        <group>case_der</group>
        <file>env_case.xml</file>
        <desc>model system name</desc>
    </entry>
