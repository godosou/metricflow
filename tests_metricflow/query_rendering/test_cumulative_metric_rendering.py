"""Tests cumulative metric query rendering by comparing rendered output against snapshot files."""

from __future__ import annotations

from typing import Mapping

import pytest
from _pytest.fixtures import FixtureRequest
from dbt_semantic_interfaces.implementations.filters.where_filter import PydanticWhereFilter
from dbt_semantic_interfaces.naming.keywords import METRIC_TIME_ELEMENT_NAME
from dbt_semantic_interfaces.test_utils import as_datetime
from dbt_semantic_interfaces.type_enums.time_granularity import TimeGranularity
from metricflow_semantics.filters.time_constraint import TimeRangeConstraint
from metricflow_semantics.query.query_parser import MetricFlowQueryParser
from metricflow_semantics.specs.column_assoc import ColumnAssociationResolver
from metricflow_semantics.specs.query_spec import MetricFlowQuerySpec
from metricflow_semantics.specs.spec_classes import EntityReference, MetricSpec, TimeDimensionSpec
from metricflow_semantics.test_helpers.config_helpers import MetricFlowTestConfiguration
from metricflow_semantics.test_helpers.metric_time_dimension import MTD_SPEC_DAY, MTD_SPEC_MONTH

from metricflow.dataflow.builder.dataflow_plan_builder import DataflowPlanBuilder
from metricflow.plan_conversion.dataflow_to_sql import DataflowToSqlQueryPlanConverter
from metricflow.protocols.sql_client import SqlClient
from tests_metricflow.fixtures.manifest_fixtures import MetricFlowEngineTestFixture, SemanticManifestSetup
from tests_metricflow.query_rendering.compare_rendered_query import convert_and_check


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a basic cumulative metric query."""
    dataflow_plan = dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="trailing_2_months_revenue"),),
            dimension_specs=(),
            time_dimension_specs=(
                TimeDimensionSpec(
                    element_name="ds",
                    entity_links=(),
                    time_granularity=TimeGranularity.DAY,
                ),
            ),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_with_time_constraint(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a cumulative metric query with an adjustable time constraint.

    Not all query inputs with time constraint filters allow us to adjust the time constraint to include the full
    span of input data for a cumulative metric, but when we receive a time constraint filter expression we can
    automatically adjust it should render a query similar to this one.
    """
    dataflow_plan = dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="trailing_2_months_revenue"),),
            dimension_specs=(),
            time_dimension_specs=(
                TimeDimensionSpec(
                    element_name="metric_time",
                    entity_links=(),
                    time_granularity=TimeGranularity.DAY,
                ),
            ),
            time_range_constraint=TimeRangeConstraint(
                start_time=as_datetime("2020-01-01"), end_time=as_datetime("2020-01-01")
            ),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_with_non_adjustable_time_filter(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    column_association_resolver: ColumnAssociationResolver,
    query_parser: MetricFlowQueryParser,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a cumulative metric query with a time filter that cannot be automatically adjusted.

    Not all query inputs with time constraint filters allow us to adjust the time constraint to include the full
    span of input data for a cumulative metric. When we do not have an adjustable time filter we must include all
    input data in order to ensure the cumulative metric is correct.
    """
    query_spec = query_parser.parse_and_validate_query(
        metric_names=("every_two_days_bookers",),
        group_by_names=(METRIC_TIME_ELEMENT_NAME,),
        where_constraint=PydanticWhereFilter(
            where_sql_template=(
                "{{ TimeDimension('metric_time', 'day') }} = '2020-01-03' "
                "or {{ TimeDimension('metric_time', 'day') }} = '2020-01-07'"
            )
        ),
    ).query_spec
    dataflow_plan = dataflow_plan_builder.build_plan(query_spec)

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_no_ds(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a cumulative metric with no time dimension specified."""
    dataflow_plan = dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="trailing_2_months_revenue"),),
            dimension_specs=(),
            time_dimension_specs=(),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_no_window(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a query where there is a windowless cumulative metric to compute."""
    dataflow_plan = dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="revenue_all_time"),),
            dimension_specs=(),
            time_dimension_specs=(
                TimeDimensionSpec(
                    element_name="ds",
                    entity_links=(),
                    time_granularity=TimeGranularity.MONTH,
                ),
            ),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_no_window_with_time_constraint(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a query for a windowless cumulative metric query with an adjustable time constraint."""
    dataflow_plan = dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="revenue_all_time"),),
            dimension_specs=(),
            time_dimension_specs=(MTD_SPEC_DAY,),
            time_range_constraint=TimeRangeConstraint(
                start_time=as_datetime("2020-01-01"), end_time=as_datetime("2020-01-01")
            ),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_grain_to_date(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a query against a grain_to_date cumulative metric."""
    dataflow_plan = dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="revenue_mtd"),),
            dimension_specs=(),
            time_dimension_specs=(
                TimeDimensionSpec(
                    element_name="ds",
                    entity_links=(),
                    time_granularity=TimeGranularity.MONTH,
                ),
            ),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_month(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    extended_date_dataflow_plan_builder: DataflowPlanBuilder,
    extended_date_dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a query for a cumulative metric based on a monthly time dimension."""
    dataflow_plan = extended_date_dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="trailing_3_months_bookings"),),
            dimension_specs=(),
            time_dimension_specs=(MTD_SPEC_MONTH,),
            time_range_constraint=TimeRangeConstraint(
                start_time=as_datetime("2020-03-05"), end_time=as_datetime("2021-01-04")
            ),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=extended_date_dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )


@pytest.mark.sql_engine_snapshot
def test_cumulative_metric_with_agg_time_dimension(
    request: FixtureRequest,
    mf_test_configuration: MetricFlowTestConfiguration,
    dataflow_plan_builder: DataflowPlanBuilder,
    dataflow_to_sql_converter: DataflowToSqlQueryPlanConverter,
    mf_engine_test_fixture_mapping: Mapping[SemanticManifestSetup, MetricFlowEngineTestFixture],
    sql_client: SqlClient,
) -> None:
    """Tests rendering a query for a cumulative metric queried with agg time dimension."""
    dataflow_plan = dataflow_plan_builder.build_plan(
        MetricFlowQuerySpec(
            metric_specs=(MetricSpec(element_name="trailing_2_months_revenue"),),
            dimension_specs=(),
            time_dimension_specs=(
                TimeDimensionSpec(element_name="ds", entity_links=(EntityReference("revenue_instance"),)),
            ),
        )
    )

    convert_and_check(
        request=request,
        mf_test_configuration=mf_test_configuration,
        dataflow_to_sql_converter=dataflow_to_sql_converter,
        sql_client=sql_client,
        node=dataflow_plan.sink_node,
    )
