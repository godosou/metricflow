from __future__ import annotations

import logging
from collections import OrderedDict
from typing import List, Optional, Sequence, Tuple, Union

from dbt_semantic_interfaces.enum_extension import assert_values_exhausted
from dbt_semantic_interfaces.naming.keywords import METRIC_TIME_ELEMENT_NAME
from dbt_semantic_interfaces.protocols.metric import MetricInputMeasure, MetricType
from dbt_semantic_interfaces.references import EntityReference, MetricModelReference
from dbt_semantic_interfaces.type_enums.aggregation_type import AggregationType
from dbt_semantic_interfaces.type_enums.conversion_calculation_type import ConversionCalculationType
from dbt_semantic_interfaces.validations.unique_valid_name import MetricFlowReservedKeywords
from metricflow_semantics.aggregation_properties import AggregationState
from metricflow_semantics.dag.id_prefix import StaticIdPrefix
from metricflow_semantics.dag.mf_dag import DagId
from metricflow_semantics.dag.sequential_id import SequentialIdGenerator
from metricflow_semantics.filters.time_constraint import TimeRangeConstraint
from metricflow_semantics.instances import (
    GroupByMetricInstance,
    InstanceSet,
    MetadataInstance,
    MetricInstance,
    TimeDimensionInstance,
)
from metricflow_semantics.mf_logging.formatting import indent
from metricflow_semantics.model.semantic_manifest_lookup import SemanticManifestLookup
from metricflow_semantics.specs.column_assoc import (
    ColumnAssociation,
    ColumnAssociationResolver,
)
from metricflow_semantics.specs.spec_classes import (
    GroupByMetricSpec,
    MeasureSpec,
    MetadataSpec,
    MetricSpec,
    TimeDimensionSpec,
)
from metricflow_semantics.specs.spec_set import InstanceSpecSet
from metricflow_semantics.sql.sql_join_type import SqlJoinType
from metricflow_semantics.time.time_constants import ISO8601_PYTHON_FORMAT

from metricflow.dataflow.dataflow_plan import (
    DataflowPlanNode,
    DataflowPlanNodeVisitor,
)
from metricflow.dataflow.nodes.add_generated_uuid import AddGeneratedUuidColumnNode
from metricflow.dataflow.nodes.aggregate_measures import AggregateMeasuresNode
from metricflow.dataflow.nodes.combine_aggregated_outputs import CombineAggregatedOutputsNode
from metricflow.dataflow.nodes.compute_metrics import ComputeMetricsNode
from metricflow.dataflow.nodes.constrain_time import ConstrainTimeRangeNode
from metricflow.dataflow.nodes.filter_elements import FilterElementsNode
from metricflow.dataflow.nodes.join_conversion_events import JoinConversionEventsNode
from metricflow.dataflow.nodes.join_over_time import JoinOverTimeRangeNode
from metricflow.dataflow.nodes.join_to_base import JoinOnEntitiesNode
from metricflow.dataflow.nodes.join_to_time_spine import JoinToTimeSpineNode
from metricflow.dataflow.nodes.metric_time_transform import MetricTimeDimensionTransformNode
from metricflow.dataflow.nodes.min_max import MinMaxNode
from metricflow.dataflow.nodes.order_by_limit import OrderByLimitNode
from metricflow.dataflow.nodes.read_sql_source import ReadSqlSourceNode
from metricflow.dataflow.nodes.semi_additive_join import SemiAdditiveJoinNode
from metricflow.dataflow.nodes.where_filter import WhereConstraintNode
from metricflow.dataflow.nodes.write_to_data_table import WriteToResultDataTableNode
from metricflow.dataflow.nodes.write_to_table import WriteToResultTableNode
from metricflow.dataset.dataset_classes import DataSet
from metricflow.dataset.sql_dataset import SqlDataSet
from metricflow.plan_conversion.convert_to_sql_plan import ConvertToSqlPlanResult
from metricflow.plan_conversion.instance_converters import (
    AddGroupByMetric,
    AddLinkToLinkableElements,
    AddMetadata,
    AddMetrics,
    AliasAggregatedMeasures,
    ChangeAssociatedColumns,
    ChangeMeasureAggregationState,
    ConvertToMetadata,
    CreateSelectColumnForCombineOutputNode,
    CreateSelectColumnsForInstances,
    CreateSelectColumnsWithMeasuresAggregated,
    CreateSqlColumnReferencesForInstances,
    FilterElements,
    FilterLinkableInstancesWithLeadingLink,
    InstanceSetTransform,
    RemoveMeasures,
    RemoveMetrics,
    UpdateMeasureFillNullsWith,
    create_select_columns_for_instance_sets,
)
from metricflow.plan_conversion.select_column_gen import (
    SelectColumnSet,
)
from metricflow.plan_conversion.spec_transforms import (
    CreateColumnAssociations,
    CreateSelectCoalescedColumnsForLinkableSpecs,
    SelectOnlyLinkableSpecs,
)
from metricflow.plan_conversion.sql_join_builder import (
    AnnotatedSqlDataSet,
    ColumnEqualityDescription,
    SqlQueryPlanJoinBuilder,
)
from metricflow.plan_conversion.time_spine import TIME_SPINE_DATA_SET_DESCRIPTION, TimeSpineSource
from metricflow.protocols.sql_client import SqlEngine
from metricflow.sql.optimizer.optimization_levels import (
    SqlQueryOptimizationLevel,
    SqlQueryOptimizerConfiguration,
)
from metricflow.sql.sql_exprs import (
    SqlAggregateFunctionExpression,
    SqlBetweenExpression,
    SqlColumnReference,
    SqlColumnReferenceExpression,
    SqlComparison,
    SqlComparisonExpression,
    SqlDateTruncExpression,
    SqlExpressionNode,
    SqlExtractExpression,
    SqlFunction,
    SqlFunctionExpression,
    SqlGenerateUuidExpression,
    SqlLogicalExpression,
    SqlLogicalOperator,
    SqlRatioComputationExpression,
    SqlStringExpression,
    SqlStringLiteralExpression,
    SqlWindowFunction,
    SqlWindowFunctionExpression,
    SqlWindowOrderByArgument,
)
from metricflow.sql.sql_plan import (
    SqlCreateTableAsNode,
    SqlJoinDescription,
    SqlOrderByDescription,
    SqlQueryPlan,
    SqlQueryPlanNode,
    SqlSelectColumn,
    SqlSelectStatementNode,
    SqlTableFromClauseNode,
)

logger = logging.getLogger(__name__)


def _make_time_range_comparison_expr(
    table_alias: str, column_alias: str, time_range_constraint: TimeRangeConstraint
) -> SqlExpressionNode:
    """Build an expression like "ds BETWEEN CAST('2020-01-01' AS TIMESTAMP) AND CAST('2020-01-02' AS TIMESTAMP)."""
    # TODO: Update when adding < day granularity support.
    return SqlBetweenExpression(
        column_arg=SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=table_alias,
                column_name=column_alias,
            )
        ),
        start_expr=SqlStringLiteralExpression(
            literal_value=time_range_constraint.start_time.strftime(ISO8601_PYTHON_FORMAT),
        ),
        end_expr=SqlStringLiteralExpression(
            literal_value=time_range_constraint.end_time.strftime(ISO8601_PYTHON_FORMAT),
        ),
    )


class DataflowToSqlQueryPlanConverter(DataflowPlanNodeVisitor[SqlDataSet]):
    """Generates an SQL query plan from a node in the a metric dataflow plan."""

    def __init__(
        self,
        column_association_resolver: ColumnAssociationResolver,
        semantic_manifest_lookup: SemanticManifestLookup,
    ) -> None:
        """Constructor.

        Args:
            column_association_resolver: controls how columns for instances are generated and used between nested
            queries.
            semantic_manifest_lookup: Self-explanatory.
        """
        self._column_association_resolver = column_association_resolver
        self._semantic_manifest_lookup = semantic_manifest_lookup
        self._metric_lookup = semantic_manifest_lookup.metric_lookup
        self._semantic_model_lookup = semantic_manifest_lookup.semantic_model_lookup
        self._time_spine_source = TimeSpineSource.create_from_manifest(semantic_manifest_lookup.semantic_manifest)

    @property
    def column_association_resolver(self) -> ColumnAssociationResolver:  # noqa: D102
        return self._column_association_resolver

    def convert_to_sql_query_plan(
        self,
        sql_engine_type: SqlEngine,
        dataflow_plan_node: DataflowPlanNode,
        optimization_level: SqlQueryOptimizationLevel = SqlQueryOptimizationLevel.O4,
        sql_query_plan_id: Optional[DagId] = None,
    ) -> ConvertToSqlPlanResult:
        """Create an SQL query plan that represents the computation up to the given dataflow plan node."""
        data_set = dataflow_plan_node.accept(self)
        sql_node: SqlQueryPlanNode = data_set.sql_node
        # TODO: Make this a more generally accessible attribute instead of checking against the
        # BigQuery-ness of the engine
        use_column_alias_in_group_by = sql_engine_type is SqlEngine.BIGQUERY

        for optimizer in SqlQueryOptimizerConfiguration.optimizers_for_level(
            optimization_level, use_column_alias_in_group_by=use_column_alias_in_group_by
        ):
            logger.info(f"Applying optimizer: {optimizer.__class__.__name__}")
            sql_node = optimizer.optimize(sql_node)
            logger.info(
                f"After applying {optimizer.__class__.__name__}, the SQL query plan is:\n"
                f"{indent(sql_node.structure_text())}"
            )

        return ConvertToSqlPlanResult(
            instance_set=data_set.instance_set,
            sql_plan=SqlQueryPlan(render_node=sql_node, plan_id=sql_query_plan_id),
        )

    def _next_unique_table_alias(self) -> str:
        """Return the next unique table alias to use in generating queries."""
        return SequentialIdGenerator.create_next_id(StaticIdPrefix.SUB_QUERY).str_value

    def _make_time_spine_data_set(
        self,
        agg_time_dimension_instance: TimeDimensionInstance,
        time_spine_source: TimeSpineSource,
        time_range_constraint: Optional[TimeRangeConstraint] = None,
    ) -> SqlDataSet:
        """Make a time spine data set, which contains all date/time values like '2020-01-01', '2020-01-02'...

        Returns a data set with a column for the agg_time_dimension requested.
        Column alias will use 'metric_time' or the agg_time_dimension name depending on which the user requested.
        """
        time_spine_instance_set = InstanceSet(time_dimension_instances=(agg_time_dimension_instance,))
        time_spine_table_alias = self._next_unique_table_alias()

        column_expr = SqlColumnReferenceExpression.from_table_and_column_names(
            table_alias=time_spine_table_alias, column_name=time_spine_source.time_column_name
        )

        select_columns: Tuple[SqlSelectColumn, ...] = ()
        apply_group_by = False
        column_alias = self.column_association_resolver.resolve_spec(agg_time_dimension_instance.spec).column_name
        # If the requested granularity matches that of the time spine, do a direct select.
        # TODO: also handle date part.
        if agg_time_dimension_instance.spec.time_granularity == time_spine_source.time_column_granularity:
            select_columns += (SqlSelectColumn(expr=column_expr, column_alias=column_alias),)
        # Otherwise, apply a DATE_TRUNC() and aggregate via group_by.
        else:
            select_columns += (
                SqlSelectColumn(
                    expr=SqlDateTruncExpression(
                        time_granularity=agg_time_dimension_instance.spec.time_granularity, arg=column_expr
                    ),
                    column_alias=column_alias,
                ),
            )
            apply_group_by = True

        return SqlDataSet(
            instance_set=time_spine_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=TIME_SPINE_DATA_SET_DESCRIPTION,
                select_columns=select_columns,
                from_source=SqlTableFromClauseNode(sql_table=time_spine_source.spine_table),
                from_source_alias=time_spine_table_alias,
                group_bys=select_columns if apply_group_by else (),
                where=(
                    _make_time_range_comparison_expr(
                        table_alias=time_spine_table_alias,
                        column_alias=time_spine_source.time_column_name,
                        time_range_constraint=time_range_constraint,
                    )
                    if time_range_constraint
                    else None
                ),
            ),
        )

    def visit_source_node(self, node: ReadSqlSourceNode) -> SqlDataSet:
        """Generate the SQL to read from the source."""
        return SqlDataSet(
            sql_select_node=node.data_set.checked_sql_select_node,
            instance_set=node.data_set.instance_set,
        )

    def visit_join_over_time_range_node(self, node: JoinOverTimeRangeNode) -> SqlDataSet:
        """Generate time range join SQL."""
        table_alias_to_instance_set: OrderedDict[str, InstanceSet] = OrderedDict()
        input_data_set = node.parent_node.accept(self)
        input_data_set_alias = self._next_unique_table_alias()

        agg_time_dimension_instance: Optional[TimeDimensionInstance] = None
        for instance in input_data_set.instance_set.time_dimension_instances:
            if instance.spec == node.time_dimension_spec_for_join:
                agg_time_dimension_instance = instance
                break
        assert (
            agg_time_dimension_instance
        ), "Specified metric time spec not found in parent data set. This should have been caught by validations."

        time_spine_data_set_alias = self._next_unique_table_alias()

        # Assemble time_spine dataset with agg_time_dimension_instance selected.
        time_spine_data_set = self._make_time_spine_data_set(
            agg_time_dimension_instance=agg_time_dimension_instance,
            time_spine_source=self._time_spine_source,
            time_range_constraint=node.time_range_constraint,
        )
        table_alias_to_instance_set[time_spine_data_set_alias] = time_spine_data_set.instance_set

        join_desc = SqlQueryPlanJoinBuilder.make_cumulative_metric_time_range_join_description(
            node=node,
            metric_data_set=AnnotatedSqlDataSet(
                data_set=input_data_set,
                alias=input_data_set_alias,
                _metric_time_column_name=input_data_set.column_association_for_time_dimension(
                    agg_time_dimension_instance.spec
                ).column_name,
            ),
            time_spine_data_set=AnnotatedSqlDataSet(
                data_set=time_spine_data_set,
                alias=time_spine_data_set_alias,
                _metric_time_column_name=time_spine_data_set.column_association_for_time_dimension(
                    agg_time_dimension_instance.spec
                ).column_name,
            ),
        )

        # Remove agg_time_dimension from input data set. It will be replaced with the time spine instance.
        modified_input_instance_set = input_data_set.instance_set.transform(
            FilterElements(exclude_specs=InstanceSpecSet(time_dimension_specs=(agg_time_dimension_instance.spec,)))
        )
        table_alias_to_instance_set[input_data_set_alias] = modified_input_instance_set

        # The output instances are the same as the input instances.
        output_instance_set = ChangeAssociatedColumns(self._column_association_resolver).transform(
            input_data_set.instance_set
        )
        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=create_select_columns_for_instance_sets(
                    self._column_association_resolver, table_alias_to_instance_set
                ),
                from_source=time_spine_data_set.checked_sql_select_node,
                from_source_alias=time_spine_data_set_alias,
                joins_descs=(join_desc,),
            ),
        )

    def visit_join_on_entities_node(self, node: JoinOnEntitiesNode) -> SqlDataSet:
        """Generates the query that realizes the behavior of the JoinToStandardOutputNode."""
        # Keep a mapping between the table aliases that would be used in the query and the MDO instances in that source.
        # e.g. when building "FROM from_table a JOIN right_table b", the value for key "a" would be the instances in
        # "from_table"
        table_alias_to_instance_set: OrderedDict[str, InstanceSet] = OrderedDict()

        # Convert the dataflow from the left node to a DataSet and add context for it to table_alias_to_instance_set
        # A DataSet is a bundle of the SQL query (in object form) and the MDO instances that the SQL query contains.
        from_data_set = node.left_node.accept(self)
        from_data_set_alias = self._next_unique_table_alias()
        table_alias_to_instance_set[from_data_set_alias] = from_data_set.instance_set

        # Build the join descriptions for the SqlQueryPlan - different from node.join_descriptions which are the join
        # descriptions from the dataflow plan.
        sql_join_descs: List[SqlJoinDescription] = []

        # The dataflow plan describes how the data sets coming from the parent nodes should be joined together. Use
        # those descriptions to convert them to join descriptions for the SQL query plan.
        for join_description in node.join_targets:
            join_on_entity = join_description.join_on_entity

            right_node_to_join: DataflowPlanNode = join_description.join_node
            right_data_set: SqlDataSet = right_node_to_join.accept(self)
            right_data_set_alias = self._next_unique_table_alias()

            sql_join_desc = SqlQueryPlanJoinBuilder.make_base_output_join_description(
                left_data_set=AnnotatedSqlDataSet(data_set=from_data_set, alias=from_data_set_alias),
                right_data_set=AnnotatedSqlDataSet(data_set=right_data_set, alias=right_data_set_alias),
                join_description=join_description,
            )
            sql_join_descs.append(sql_join_desc)

            if join_on_entity:
                # Remove the linkable instances with the join_on_entity as the leading link as the next step adds the
                # link. This is to avoid cases where there is a primary entity and a dimension in the data set, and we
                # create an instance in the next step that has the same entity link.
                # e.g. a data set has the dimension "listing__country_latest" and "listing" is a primary entity in the
                # data set. The next step would create an instance like "listing__listing__country_latest" without this
                # filter.
                right_data_set_instance_set_filtered = FilterLinkableInstancesWithLeadingLink(
                    entity_link=join_on_entity,
                ).transform(right_data_set.instance_set)

                # After the right data set is joined to the "from" data set, we need to change the links for some of the
                # instances that represent the right data set. For example, if the "from" data set contains the "bookings"
                # measure instance and the right dataset contains the "country" dimension instance, then after the join,
                # the output data set should have the "country" dimension instance with the "user_id" entity link
                # (if "user_id" equality was the join condition). "country" -> "user_id__country"
                right_data_set_instance_set_after_join = right_data_set_instance_set_filtered.transform(
                    AddLinkToLinkableElements(join_on_entity=join_on_entity)
                )
            else:
                right_data_set_instance_set_after_join = right_data_set.instance_set
            table_alias_to_instance_set[right_data_set_alias] = right_data_set_instance_set_after_join

        from_data_set_output_instance_set = from_data_set.instance_set.transform(
            FilterElements(include_specs=from_data_set.instance_set.spec_set)
        )

        # Change the aggregation state for the measures to be partially aggregated if it was previously aggregated
        # since we removed the entities and added the dimensions. The dimensions could have the same value for
        # multiple rows, so we'll need to re-aggregate.
        from_data_set_output_instance_set = from_data_set_output_instance_set.transform(
            ChangeMeasureAggregationState(
                {
                    AggregationState.NON_AGGREGATED: AggregationState.NON_AGGREGATED,
                    AggregationState.COMPLETE: AggregationState.PARTIAL,
                    AggregationState.PARTIAL: AggregationState.PARTIAL,
                }
            )
        )

        table_alias_to_instance_set[from_data_set_alias] = from_data_set_output_instance_set

        # Construct the data set that contains the updated instances and the SQL nodes that should go in the various
        # clauses.
        return SqlDataSet(
            instance_set=InstanceSet.merge(list(table_alias_to_instance_set.values())),
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=create_select_columns_for_instance_sets(
                    self._column_association_resolver, table_alias_to_instance_set
                ),
                from_source=from_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
                joins_descs=tuple(sql_join_descs),
            ),
        )

    def visit_aggregate_measures_node(self, node: AggregateMeasuresNode) -> SqlDataSet:
        """Generates the query that realizes the behavior of AggregateMeasuresNode.

        This will produce a query that aggregates all measures from a given input semantic model per the
        measure spec

        In the event the input aggregations are applied to measures with aliases set, in case of, e.g.,
        a constraint applied to one instance of the measure but not another one, this method will
        apply the rename in the select statement for this node, and propagate that further along via an
        instance set transform to rename the measures.

        Any node operating on the output of this node will need to use the measure aliases instead of
        the measure names as references.

        """
        # Get the data from the parent, and change measure instances to the aggregated state.
        from_data_set: SqlDataSet = node.parent_node.accept(self)
        aggregated_instance_set = from_data_set.instance_set.transform(
            ChangeMeasureAggregationState(
                {
                    AggregationState.NON_AGGREGATED: AggregationState.COMPLETE,
                    AggregationState.COMPLETE: AggregationState.COMPLETE,
                    AggregationState.PARTIAL: AggregationState.COMPLETE,
                }
            )
        )
        # Also, the columns should always follow the resolver format.
        aggregated_instance_set = aggregated_instance_set.transform(
            ChangeAssociatedColumns(self._column_association_resolver)
        )

        # Add fill null property to corresponding measure spec
        aggregated_instance_set = aggregated_instance_set.transform(
            UpdateMeasureFillNullsWith(metric_input_measure_specs=node.metric_input_measure_specs)
        )
        from_data_set_alias = self._next_unique_table_alias()

        # Convert the instance set into a set of select column statements with updated aliases
        # Note any measure with an alias requirement will be recast at this point, and
        # downstream consumers of the resulting node must therefore request aggregated measures
        # by their appropriate aliases
        select_column_set: SelectColumnSet = aggregated_instance_set.transform(
            CreateSelectColumnsWithMeasuresAggregated(
                table_alias=from_data_set_alias,
                column_resolver=self._column_association_resolver,
                semantic_model_lookup=self._semantic_model_lookup,
                metric_input_measure_specs=node.metric_input_measure_specs,
            )
        )

        if any((spec.alias for spec in node.metric_input_measure_specs)):
            # This is a little silly, but we need to update the column instance set with the new aliases
            # There are a number of refactoring options - simplest is to consolidate this with
            # ChangeMeasureAggregationState, assuming there are no ordering dependencies up above
            aggregated_instance_set = aggregated_instance_set.transform(
                AliasAggregatedMeasures(metric_input_measure_specs=node.metric_input_measure_specs)
            )
            # and make sure we follow the resolver format for any newly aliased measures....
            aggregated_instance_set = aggregated_instance_set.transform(
                ChangeAssociatedColumns(self._column_association_resolver)
            )

        return SqlDataSet(
            instance_set=aggregated_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                # This will generate expressions with the appropriate aggregation functions e.g. SUM()
                select_columns=select_column_set.as_tuple(),
                from_source=from_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
                # This will generate expressions to group by the columns that don't correspond to a measure instance.
                group_bys=select_column_set.without_measure_columns().as_tuple(),
            ),
        )

    def visit_compute_metrics_node(self, node: ComputeMetricsNode) -> SqlDataSet:
        """Generates the query that realizes the behavior of ComputeMetricsNode."""
        from_data_set: SqlDataSet = node.parent_node.accept(self)
        from_data_set_alias = self._next_unique_table_alias()

        # TODO: Check that all measures for the metrics are in the input instance set
        # The desired output instance set has no measures, so create a copy with those removed.
        output_instance_set: InstanceSet = from_data_set.instance_set.transform(RemoveMeasures())

        # Also, the output columns should always follow the resolver format.
        output_instance_set = output_instance_set.transform(ChangeAssociatedColumns(self._column_association_resolver))
        output_instance_set = output_instance_set.transform(RemoveMetrics())

        if node.for_group_by_source_node:
            assert (
                len(node.metric_specs) == 1 and len(output_instance_set.entity_instances) == 1
            ), "Group by metrics currently only support exactly one metric grouped by exactly one entity."

        non_metric_select_column_set: SelectColumnSet = output_instance_set.transform(
            CreateSelectColumnsForInstances(
                table_alias=from_data_set_alias,
                column_resolver=self._column_association_resolver,
            )
        )

        # Add select columns that would compute the metrics to the select columns.
        metric_select_columns = []
        metric_instances = []
        group_by_metric_instance: Optional[GroupByMetricInstance] = None
        for metric_spec in node.metric_specs:
            metric = self._metric_lookup.get_metric(metric_spec.reference)

            metric_expr: Optional[SqlExpressionNode] = None
            input_measure: Optional[MetricInputMeasure] = None
            if metric.type is MetricType.RATIO:
                numerator = metric.type_params.numerator
                denominator = metric.type_params.denominator
                assert (
                    numerator is not None and denominator is not None
                ), "Missing numerator or denominator for ratio metric, this should have been caught in validation!"
                numerator_column_name = self._column_association_resolver.resolve_spec(
                    MetricSpec.from_reference(numerator.post_aggregation_reference)
                ).column_name
                denominator_column_name = self._column_association_resolver.resolve_spec(
                    MetricSpec.from_reference(denominator.post_aggregation_reference)
                ).column_name

                metric_expr = SqlRatioComputationExpression(
                    numerator=SqlColumnReferenceExpression(
                        SqlColumnReference(
                            table_alias=from_data_set_alias,
                            column_name=numerator_column_name,
                        )
                    ),
                    denominator=SqlColumnReferenceExpression(
                        SqlColumnReference(
                            table_alias=from_data_set_alias,
                            column_name=denominator_column_name,
                        )
                    ),
                )
            elif metric.type is MetricType.SIMPLE:
                if len(metric.input_measures) > 0:
                    assert (
                        len(metric.input_measures) == 1
                    ), "Simple metrics should always source from exactly 1 measure."
                    input_measure = metric.input_measures[0]
                    expr = self._column_association_resolver.resolve_spec(
                        MeasureSpec(element_name=input_measure.post_aggregation_measure_reference.element_name)
                    ).column_name
                else:
                    expr = metric.name
                metric_expr = self.__make_col_reference_or_coalesce_expr(
                    column_name=expr, input_measure=input_measure, from_data_set_alias=from_data_set_alias
                )
            elif metric.type is MetricType.CUMULATIVE:
                assert (
                    len(metric.measure_references) == 1
                ), "Cumulative metrics should always source from exactly 1 measure."
                input_measure = metric.input_measures[0]
                expr = self._column_association_resolver.resolve_spec(
                    MeasureSpec(element_name=input_measure.post_aggregation_measure_reference.element_name)
                ).column_name
                metric_expr = self.__make_col_reference_or_coalesce_expr(
                    column_name=expr, input_measure=input_measure, from_data_set_alias=from_data_set_alias
                )
            elif metric.type is MetricType.DERIVED:
                assert (
                    metric.type_params.expr
                ), "Derived metrics are required to have an `expr` in their YAML definition."
                metric_expr = SqlStringExpression(sql_expr=metric.type_params.expr)
            elif metric.type == MetricType.CONVERSION:
                conversion_type_params = metric.type_params.conversion_type_params
                assert (
                    conversion_type_params
                ), "A conversion metric should have type_params.conversion_type_params defined."
                base_measure = conversion_type_params.base_measure
                conversion_measure = conversion_type_params.conversion_measure
                base_measure_column = self._column_association_resolver.resolve_spec(
                    MeasureSpec(element_name=base_measure.post_aggregation_measure_reference.element_name)
                ).column_name
                conversion_measure_column = self._column_association_resolver.resolve_spec(
                    MeasureSpec(element_name=conversion_measure.post_aggregation_measure_reference.element_name)
                ).column_name

                calculation_type = conversion_type_params.calculation
                conversion_column_reference = SqlColumnReferenceExpression(
                    SqlColumnReference(
                        table_alias=from_data_set_alias,
                        column_name=conversion_measure_column,
                    )
                )
                base_column_reference = SqlColumnReferenceExpression(
                    SqlColumnReference(
                        table_alias=from_data_set_alias,
                        column_name=base_measure_column,
                    )
                )
                if calculation_type == ConversionCalculationType.CONVERSION_RATE:
                    metric_expr = SqlRatioComputationExpression(
                        numerator=conversion_column_reference,
                        denominator=base_column_reference,
                    )
                elif calculation_type == ConversionCalculationType.CONVERSIONS:
                    metric_expr = conversion_column_reference
            else:
                assert_values_exhausted(metric.type)

            assert metric_expr

            defined_from = MetricModelReference(metric_name=metric_spec.element_name)

            if node.for_group_by_source_node:
                entity_spec = output_instance_set.entity_instances[0].spec
                group_by_metric_spec = GroupByMetricSpec(
                    element_name=metric_spec.element_name,
                    entity_links=(),
                    metric_subquery_entity_links=entity_spec.entity_links + (entity_spec.reference,),
                )
                output_column_association = self._column_association_resolver.resolve_spec(group_by_metric_spec)
                group_by_metric_instance = GroupByMetricInstance(
                    associated_columns=(output_column_association,),
                    defined_from=defined_from,
                    spec=group_by_metric_spec,
                )
            else:
                output_column_association = self._column_association_resolver.resolve_spec(metric_spec)
                metric_instances.append(
                    MetricInstance(
                        associated_columns=(output_column_association,),
                        defined_from=defined_from,
                        spec=metric_spec,
                    )
                )
            metric_select_columns.append(
                SqlSelectColumn(expr=metric_expr, column_alias=output_column_association.column_name)
            )

        transform_func: InstanceSetTransform = AddMetrics(metric_instances)
        if group_by_metric_instance:
            transform_func = AddGroupByMetric(group_by_metric_instance)

        output_instance_set = output_instance_set.transform(transform_func)

        combined_select_column_set = non_metric_select_column_set.merge(
            SelectColumnSet(metric_columns=metric_select_columns)
        )

        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=combined_select_column_set.as_tuple(),
                from_source=from_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
            ),
        )

    def __make_col_reference_or_coalesce_expr(
        self, column_name: str, input_measure: Optional[MetricInputMeasure], from_data_set_alias: str
    ) -> SqlExpressionNode:
        # Use a column reference to improve query optimization.
        metric_expr: SqlExpressionNode = SqlColumnReferenceExpression(
            SqlColumnReference(table_alias=from_data_set_alias, column_name=column_name)
        )
        # Coalesce nulls to requested integer value, if requested.
        if input_measure and input_measure.fill_nulls_with is not None:
            metric_expr = SqlAggregateFunctionExpression(
                sql_function=SqlFunction.COALESCE,
                sql_function_args=[metric_expr, SqlStringExpression(str(input_measure.fill_nulls_with))],
            )
        return metric_expr

    def visit_order_by_limit_node(self, node: OrderByLimitNode) -> SqlDataSet:  # noqa: D102
        from_data_set: SqlDataSet = node.parent_node.accept(self)
        output_instance_set = from_data_set.instance_set
        from_data_set_alias = self._next_unique_table_alias()

        # Also, the output columns should always follow the resolver format.
        output_instance_set = output_instance_set.transform(ChangeAssociatedColumns(self._column_association_resolver))

        order_by_descriptions = []
        for order_by_spec in node.order_by_specs:
            order_by_descriptions.append(
                SqlOrderByDescription(
                    expr=SqlColumnReferenceExpression(
                        col_ref=SqlColumnReference(
                            table_alias=from_data_set_alias,
                            column_name=self._column_association_resolver.resolve_spec(
                                order_by_spec.instance_spec
                            ).column_name,
                        )
                    ),
                    desc=order_by_spec.descending,
                )
            )

        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                # This creates select expressions for all columns referenced in the instance set.
                select_columns=output_instance_set.transform(
                    CreateSelectColumnsForInstances(from_data_set_alias, self._column_association_resolver)
                ).as_tuple(),
                from_source=from_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
                order_bys=tuple(order_by_descriptions),
                limit=node.limit,
            ),
        )

    def visit_write_to_result_data_table_node(self, node: WriteToResultDataTableNode) -> SqlDataSet:  # noqa: D102
        # Returning the parent-node SQL as an approximation since you can't write to a data_table via SQL.
        return node.parent_node.accept(self)

    def visit_write_to_result_table_node(self, node: WriteToResultTableNode) -> SqlDataSet:  # noqa: D102
        input_data_set: SqlDataSet = node.parent_node.accept(self)
        input_instance_set: InstanceSet = input_data_set.instance_set
        return SqlDataSet(
            instance_set=input_instance_set,
            sql_node=SqlCreateTableAsNode(
                sql_table=node.output_sql_table,
                parent_node=input_data_set.checked_sql_select_node,
            ),
        )

    def visit_filter_elements_node(self, node: FilterElementsNode) -> SqlDataSet:
        """Generates the query that realizes the behavior of FilterElementsNode."""
        from_data_set: SqlDataSet = node.parent_node.accept(self)
        output_instance_set = from_data_set.instance_set.transform(FilterElements(node.include_specs))
        from_data_set_alias = self._next_unique_table_alias()

        # Also, the output columns should always follow the resolver format.
        output_instance_set = output_instance_set.transform(ChangeAssociatedColumns(self._column_association_resolver))

        # This creates select expressions for all columns referenced in the instance set.
        select_columns = output_instance_set.transform(
            CreateSelectColumnsForInstances(from_data_set_alias, self._column_association_resolver)
        ).as_tuple()

        # If distinct values requested, group by all select columns.
        group_bys = select_columns if node.distinct else ()
        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=select_columns,
                from_source=from_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
                group_bys=group_bys,
            ),
        )

    def visit_where_constraint_node(self, node: WhereConstraintNode) -> SqlDataSet:
        """Adds where clause to SQL statement from parent node."""
        parent_data_set: SqlDataSet = node.parent_node.accept(self)
        # Since we're copying the instance set from the parent to conveniently generate the output instance set for this
        # node, we'll need to change the column names.
        output_instance_set = parent_data_set.instance_set.transform(
            ChangeAssociatedColumns(self._column_association_resolver)
        )
        from_data_set_alias = self._next_unique_table_alias()

        column_associations_in_where_sql: Sequence[ColumnAssociation] = CreateColumnAssociations(
            column_association_resolver=self._column_association_resolver
        ).transform(spec_set=InstanceSpecSet.create_from_specs(node.where.linkable_specs))

        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                # This creates select expressions for all columns referenced in the instance set.
                select_columns=output_instance_set.transform(
                    CreateSelectColumnsForInstances(from_data_set_alias, self._column_association_resolver)
                ).as_tuple(),
                from_source=parent_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
                where=SqlStringExpression(
                    sql_expr=node.where.where_sql,
                    used_columns=tuple(
                        column_association.column_name for column_association in column_associations_in_where_sql
                    ),
                    bind_parameters=node.where.bind_parameters,
                ),
            ),
        )

    def visit_combine_aggregated_outputs_node(self, node: CombineAggregatedOutputsNode) -> SqlDataSet:
        """Join aggregated output datasets together to return a single dataset containing all metrics/measures.

        This node may exist in one of two situations: when metrics/measures need to be combined in order to produce a single
        dataset with all required inputs for a metric (ie., derived metric), or when metrics need to be combined in order to
        produce a single dataset of output for downstream consumption by the end user.

        The join key will be a coalesced set of all previously seen dimension values. For example:
            FROM (
              ...
            ) subq_9
            FULL OUTER JOIN (
              ...
            ) subq_10
            ON
              subq_9.is_instant = subq_10.is_instant
              AND subq_9.ds = subq_10.ds
            FULL OUTER JOIN (
              ...
            ) subq_11
            ON
              COALESCE(subq_9.is_instant, subq_10.is_instant) = subq_11.is_instant
              AND COALESCE(subq_9.ds, subq_10.ds) = subq_11.ds

        Whenever these nodes are joined using a FULL OUTER JOIN, we must also do a subsequent re-aggregation pass to
        deduplicate the dimension value outputs across different metrics. This can happen if one or more of the
        dimensions contains a NULL value. In that case, the FULL OUTER JOIN condition will fail, because NULL = NULL
        returns NULL. Unfortunately, there's no way to do a robust NULL-safe comparison across engines in a FULL
        OUTER JOIN context, because many engines do not support complex ON conditions or other techniques we might
        use to apply a sentinel value for NULL to NULL comparisons.
        """
        assert (
            len(node.parent_nodes) > 1
        ), "Shouldn't have a CombineAggregatedOutputsNode in the dataflow plan if there's only 1 parent."

        parent_data_sets: List[AnnotatedSqlDataSet] = []
        table_alias_to_instance_set: OrderedDict[str, InstanceSet] = OrderedDict()

        for parent_node in node.parent_nodes:
            parent_sql_data_set = parent_node.accept(self)
            table_alias = self._next_unique_table_alias()
            parent_data_sets.append(AnnotatedSqlDataSet(data_set=parent_sql_data_set, alias=table_alias))
            table_alias_to_instance_set[table_alias] = parent_sql_data_set.instance_set

        # When we create the components of the join that combines metrics it will be one of INNER, FULL OUTER,
        # or CROSS JOIN. Order doesn't matter for these join types, so we will use the first element in the FROM
        # clause and create join descriptions from the rest.
        from_data_set = parent_data_sets[0]
        join_data_sets = parent_data_sets[1:]

        # Sanity check that all parents have the same linkable specs before building the join descriptions.
        linkable_specs = from_data_set.data_set.instance_set.spec_set.linkable_specs
        assert all(
            [set(x.data_set.instance_set.spec_set.linkable_specs) == set(linkable_specs) for x in join_data_sets]
        ), "All parent nodes should have the same set of linkable instances since all values are coalesced."

        linkable_spec_set = from_data_set.data_set.instance_set.spec_set.transform(SelectOnlyLinkableSpecs())
        join_type = SqlJoinType.CROSS_JOIN if len(linkable_spec_set.all_specs) == 0 else SqlJoinType.FULL_OUTER

        joins_descriptions: List[SqlJoinDescription] = []
        # TODO: refactor this loop into SqlQueryPlanJoinBuilder
        column_associations = tuple(
            self._column_association_resolver.resolve_spec(spec) for spec in linkable_spec_set.all_specs
        )
        column_names = tuple(association.column_name for association in column_associations)
        aliases_seen = [from_data_set.alias]
        for join_data_set in join_data_sets:
            joins_descriptions.append(
                SqlQueryPlanJoinBuilder.make_join_description_for_combining_datasets(
                    from_data_set=from_data_set,
                    join_data_set=join_data_set,
                    join_type=join_type,
                    column_names=column_names,
                    table_aliases_for_coalesce=aliases_seen,
                )
            )
            aliases_seen.append(join_data_set.alias)

        # We can merge all parent instances since the common linkable instances will be de-duped.
        output_instance_set = InstanceSet.merge([x.data_set.instance_set for x in parent_data_sets])
        output_instance_set = output_instance_set.transform(ChangeAssociatedColumns(self._column_association_resolver))

        aggregated_select_columns = SelectColumnSet()
        for table_alias, instance_set in table_alias_to_instance_set.items():
            aggregated_select_columns = aggregated_select_columns.merge(
                instance_set.transform(
                    CreateSelectColumnForCombineOutputNode(
                        table_alias=table_alias,
                        column_resolver=self._column_association_resolver,
                        metric_lookup=self._metric_lookup,
                    )
                )
            )
        linkable_select_column_set = linkable_spec_set.transform(
            CreateSelectCoalescedColumnsForLinkableSpecs(
                column_association_resolver=self._column_association_resolver,
                table_aliases=[x.alias for x in parent_data_sets],
            )
        )
        combined_select_column_set = linkable_select_column_set.merge(aggregated_select_columns)

        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=combined_select_column_set.as_tuple(),
                from_source=from_data_set.data_set.checked_sql_select_node,
                from_source_alias=from_data_set.alias,
                joins_descs=tuple(joins_descriptions),
                group_bys=linkable_select_column_set.as_tuple(),
            ),
        )

    def visit_constrain_time_range_node(self, node: ConstrainTimeRangeNode) -> SqlDataSet:
        """Convert ConstrainTimeRangeNode to a SqlDataSet by building the time constraint comparison.

        Use the smallest time granularity to build the comparison since that's what was used in the semantic model
        definition and it wouldn't have a DATE_TRUNC() in the expression. We want to build this:

            ds >= '2020-01-01' AND ds <= '2020-02-01'

        instead of this: DATE_TRUNC('month', ds) >= '2020-01-01' AND DATE_TRUNC('month', ds <= '2020-02-01')
        """
        from_data_set: SqlDataSet = node.parent_node.accept(self)
        from_data_set_alias = self._next_unique_table_alias()

        time_dimension_instances_for_metric_time = sorted(
            from_data_set.metric_time_dimension_instances,
            key=lambda x: x.spec.time_granularity.to_int(),
        )

        assert (
            len(time_dimension_instances_for_metric_time) > 0
        ), "No metric time dimensions found in the input data set for this node"

        time_dimension_instance_for_metric_time = time_dimension_instances_for_metric_time[0]

        # Build an expression like "ds >= CAST('2020-01-01' AS TIMESTAMP) AND ds <= CAST('2020-01-02' AS TIMESTAMP)"
        constrain_metric_time_column_condition = _make_time_range_comparison_expr(
            table_alias=from_data_set_alias,
            column_alias=time_dimension_instance_for_metric_time.associated_column.column_name,
            time_range_constraint=node.time_range_constraint,
        )

        output_instance_set = from_data_set.instance_set
        # Output columns should always follow the resolver format.
        output_instance_set = output_instance_set.transform(ChangeAssociatedColumns(self._column_association_resolver))

        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                # This creates select expressions for all columns referenced in the instance set.
                select_columns=output_instance_set.transform(
                    CreateSelectColumnsForInstances(from_data_set_alias, self._column_association_resolver)
                ).as_tuple(),
                from_source=from_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
                where=constrain_metric_time_column_condition,
            ),
        )

    def visit_metric_time_dimension_transform_node(self, node: MetricTimeDimensionTransformNode) -> SqlDataSet:
        """Implement the behavior of the MetricTimeDimensionTransformNode.

        This node will create an output data set that is similar to the input data set, but the measure instances it
        contains is a subset of the input data set. Only measure instances that have an aggregation time dimension
        matching the one defined in the node will be passed. In addition, an additional time dimension instance for
        "metric time" will be included. See DataSet.metric_time_dimension_reference().
        """
        input_data_set: SqlDataSet = node.parent_node.accept(self)

        # Find which measures have an aggregation time dimension that is the same as the one specified in the node.
        # Only these measures will be in the output data set.
        output_measure_instances = []
        for measure_instance in input_data_set.instance_set.measure_instances:
            semantic_model = self._semantic_model_lookup.get_by_reference(
                semantic_model_reference=measure_instance.origin_semantic_model_reference.semantic_model_reference
            )
            assert semantic_model is not None, (
                f"{measure_instance} was defined from "
                f"{measure_instance.origin_semantic_model_reference.semantic_model_reference}, but that can't be found"
            )
            aggregation_time_dimension_for_measure = semantic_model.checked_agg_time_dimension_for_measure(
                measure_reference=measure_instance.spec.reference
            )
            if aggregation_time_dimension_for_measure == node.aggregation_time_dimension_reference:
                output_measure_instances.append(measure_instance)

        # Find time dimension instances that refer to the same dimension as the one specified in the node.
        matching_time_dimension_instances = []
        for time_dimension_instance in input_data_set.instance_set.time_dimension_instances:
            # The specification for the time dimension to use for aggregation is the local one.
            if (
                len(time_dimension_instance.spec.entity_links) == 0
                and time_dimension_instance.spec.reference == node.aggregation_time_dimension_reference
            ):
                matching_time_dimension_instances.append(time_dimension_instance)

        output_time_dimension_instances: List[TimeDimensionInstance] = []
        output_time_dimension_instances.extend(input_data_set.instance_set.time_dimension_instances)
        output_column_to_input_column: OrderedDict[str, str] = OrderedDict()

        # For those matching time dimension instances, create the analog metric time dimension instances for the output.
        for matching_time_dimension_instance in matching_time_dimension_instances:
            metric_time_dimension_spec = DataSet.metric_time_dimension_spec(
                time_granularity=matching_time_dimension_instance.spec.time_granularity,
                date_part=matching_time_dimension_instance.spec.date_part,
            )
            metric_time_dimension_column_association = self._column_association_resolver.resolve_spec(
                metric_time_dimension_spec
            )
            output_time_dimension_instances.append(
                TimeDimensionInstance(
                    defined_from=matching_time_dimension_instance.defined_from,
                    associated_columns=(self._column_association_resolver.resolve_spec(metric_time_dimension_spec),),
                    spec=metric_time_dimension_spec,
                )
            )
            output_column_to_input_column[
                metric_time_dimension_column_association.column_name
            ] = matching_time_dimension_instance.associated_column.column_name

        output_instance_set = InstanceSet(
            measure_instances=tuple(output_measure_instances),
            dimension_instances=input_data_set.instance_set.dimension_instances,
            time_dimension_instances=tuple(output_time_dimension_instances),
            entity_instances=input_data_set.instance_set.entity_instances,
            metric_instances=input_data_set.instance_set.metric_instances,
        )
        output_instance_set = ChangeAssociatedColumns(self._column_association_resolver).transform(output_instance_set)

        from_data_set_alias = self._next_unique_table_alias()

        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                # This creates select expressions for all columns referenced in the instance set.
                select_columns=CreateSelectColumnsForInstances(
                    column_resolver=self._column_association_resolver,
                    table_alias=from_data_set_alias,
                    output_to_input_column_mapping=output_column_to_input_column,
                )
                .transform(output_instance_set)
                .as_tuple(),
                from_source=input_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
            ),
        )

    def visit_semi_additive_join_node(self, node: SemiAdditiveJoinNode) -> SqlDataSet:
        """Implements the behaviour of SemiAdditiveJoinNode.

        This node will get the build a data set row filtered by the aggregate function on the
        specified dimension that is non-additive. Then that dataset would be joined with the input data
        on that dimension along with grouping by entities that are also passed in.
        """
        from_data_set: SqlDataSet = node.parent_node.accept(self)

        from_data_set_alias = self._next_unique_table_alias()

        # Get the output_instance_set of the parent_node
        output_instance_set = from_data_set.instance_set
        output_instance_set = output_instance_set.transform(ChangeAssociatedColumns(self._column_association_resolver))

        # Build the JoinDescriptions to handle the row base filtering on the output_data_set
        inner_join_data_set_alias = self._next_unique_table_alias()

        column_equality_descriptions: List[ColumnEqualityDescription] = []

        # Build Time Dimension SqlSelectColumn
        time_dimension_column_name = self.column_association_resolver.resolve_spec(node.time_dimension_spec).column_name
        join_time_dimension_column_name = self.column_association_resolver.resolve_spec(
            node.time_dimension_spec.with_aggregation_state(AggregationState.COMPLETE),
        ).column_name
        time_dimension_select_column = SqlSelectColumn(
            expr=SqlFunctionExpression.build_expression_from_aggregation_type(
                aggregation_type=node.agg_by_function,
                sql_column_expression=SqlColumnReferenceExpression(
                    SqlColumnReference(
                        table_alias=inner_join_data_set_alias,
                        column_name=time_dimension_column_name,
                    ),
                ),
            ),
            column_alias=join_time_dimension_column_name,
        )
        column_equality_descriptions.append(
            ColumnEqualityDescription(
                left_column_alias=time_dimension_column_name,
                right_column_alias=join_time_dimension_column_name,
            )
        )

        # Build optional window grouping SqlSelectColumn
        entity_select_columns: List[SqlSelectColumn] = []
        for entity_spec in node.entity_specs:
            entity_column_name = self.column_association_resolver.resolve_spec(entity_spec).column_name
            entity_select_columns.append(
                SqlSelectColumn(
                    expr=SqlColumnReferenceExpression(
                        SqlColumnReference(
                            table_alias=inner_join_data_set_alias,
                            column_name=entity_column_name,
                        ),
                    ),
                    column_alias=entity_column_name,
                )
            )
            column_equality_descriptions.append(
                ColumnEqualityDescription(
                    left_column_alias=entity_column_name,
                    right_column_alias=entity_column_name,
                )
            )

        # Propogate additional group by during query time of the non-additive time dimension
        queried_time_dimension_select_column: Optional[SqlSelectColumn] = None
        if node.queried_time_dimension_spec:
            query_time_dimension_column_name = self.column_association_resolver.resolve_spec(
                node.queried_time_dimension_spec
            ).column_name
            queried_time_dimension_select_column = SqlSelectColumn(
                expr=SqlColumnReferenceExpression(
                    SqlColumnReference(
                        table_alias=inner_join_data_set_alias,
                        column_name=query_time_dimension_column_name,
                    ),
                ),
                column_alias=query_time_dimension_column_name,
            )

        row_filter_group_bys = tuple(entity_select_columns)
        if queried_time_dimension_select_column:
            row_filter_group_bys += (queried_time_dimension_select_column,)
        # Construct SelectNode for Row filtering
        row_filter_sql_select_node = SqlSelectStatementNode(
            description=f"Filter row on {node.agg_by_function.name}({time_dimension_column_name})",
            select_columns=row_filter_group_bys + (time_dimension_select_column,),
            from_source=from_data_set.checked_sql_select_node,
            from_source_alias=inner_join_data_set_alias,
            group_bys=row_filter_group_bys,
        )

        join_data_set_alias = self._next_unique_table_alias()
        sql_join_desc = SqlQueryPlanJoinBuilder.make_column_equality_sql_join_description(
            right_source_node=row_filter_sql_select_node,
            left_source_alias=from_data_set_alias,
            right_source_alias=join_data_set_alias,
            column_equality_descriptions=column_equality_descriptions,
            join_type=SqlJoinType.INNER,
        )
        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=output_instance_set.transform(
                    CreateSelectColumnsForInstances(from_data_set_alias, self._column_association_resolver)
                ).as_tuple(),
                from_source=from_data_set.checked_sql_select_node,
                from_source_alias=from_data_set_alias,
                joins_descs=(sql_join_desc,),
            ),
        )

    def visit_join_to_time_spine_node(self, node: JoinToTimeSpineNode) -> SqlDataSet:  # noqa: D102
        parent_data_set = node.parent_node.accept(self)
        parent_alias = self._next_unique_table_alias()

        if node.use_custom_agg_time_dimension:
            agg_time_dimension = node.requested_agg_time_dimension_specs[0]
            agg_time_element_name = agg_time_dimension.element_name
            agg_time_entity_links: Tuple[EntityReference, ...] = agg_time_dimension.entity_links
        else:
            agg_time_element_name = METRIC_TIME_ELEMENT_NAME
            agg_time_entity_links = ()

        # Find the time dimension instances in the parent data set that match the one we want to join with.
        agg_time_dimension_instances: List[TimeDimensionInstance] = []
        for instance in parent_data_set.instance_set.time_dimension_instances:
            if (
                instance.spec.date_part is None  # Ensure we don't join using an instance with date part
                and instance.spec.element_name == agg_time_element_name
                and instance.spec.entity_links == agg_time_entity_links
            ):
                agg_time_dimension_instances.append(instance)

        # Choose the instance with the smallest granularity available.
        agg_time_dimension_instances.sort(key=lambda instance: instance.spec.time_granularity.to_int())
        assert len(agg_time_dimension_instances) > 0, (
            "Couldn't find requested agg_time_dimension in parent data set. The dataflow plan may have been "
            "configured incorrectly."
        )
        agg_time_dimension_instance_for_join = agg_time_dimension_instances[0]

        # Build time spine data set using the requested agg_time_dimension name.
        time_spine_alias = self._next_unique_table_alias()
        time_spine_dataset = self._make_time_spine_data_set(
            agg_time_dimension_instance=agg_time_dimension_instance_for_join,
            time_spine_source=self._time_spine_source,
            time_range_constraint=node.time_range_constraint,
        )

        # Build join expression.

        join_description = SqlQueryPlanJoinBuilder.make_join_to_time_spine_join_description(
            node=node,
            time_spine_alias=time_spine_alias,
            agg_time_dimension_column_name=self.column_association_resolver.resolve_spec(
                agg_time_dimension_instance_for_join.spec
            ).column_name,
            parent_sql_select_node=parent_data_set.checked_sql_select_node,
            parent_alias=parent_alias,
        )

        # Select all instances from the parent data set, EXCEPT agg_time_dimensions.
        # The agg_time_dimensions will be selected from the time spine data set.
        time_dimensions_to_select_from_parent: Tuple[TimeDimensionInstance, ...] = ()
        time_dimensions_to_select_from_time_spine: Tuple[TimeDimensionInstance, ...] = ()
        for time_dimension_instance in parent_data_set.instance_set.time_dimension_instances:
            if (
                time_dimension_instance.spec.element_name == agg_time_element_name
                and time_dimension_instance.spec.entity_links == agg_time_entity_links
            ):
                time_dimensions_to_select_from_time_spine += (time_dimension_instance,)
            else:
                time_dimensions_to_select_from_parent += (time_dimension_instance,)
        parent_instance_set = InstanceSet(
            measure_instances=parent_data_set.instance_set.measure_instances,
            dimension_instances=parent_data_set.instance_set.dimension_instances,
            time_dimension_instances=tuple(
                time_dimension_instance
                for time_dimension_instance in parent_data_set.instance_set.time_dimension_instances
                if not (
                    time_dimension_instance.spec.element_name == agg_time_element_name
                    and time_dimension_instance.spec.entity_links == agg_time_entity_links
                )
            ),
            entity_instances=parent_data_set.instance_set.entity_instances,
            metric_instances=parent_data_set.instance_set.metric_instances,
            metadata_instances=parent_data_set.instance_set.metadata_instances,
        )
        parent_select_columns = create_select_columns_for_instance_sets(
            self._column_association_resolver, OrderedDict({parent_alias: parent_instance_set})
        )

        # Select agg_time_dimension instance from time spine data set.
        assert (
            len(time_spine_dataset.instance_set.time_dimension_instances) == 1
            and len(time_spine_dataset.checked_sql_select_node.select_columns) == 1
        ), "Time spine dataset not configured properly. Expected exactly one column."
        original_time_spine_dim_instance = time_spine_dataset.instance_set.time_dimension_instances[0]
        time_spine_column_select_expr: Union[
            SqlColumnReferenceExpression, SqlDateTruncExpression
        ] = SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=time_spine_alias, column_name=original_time_spine_dim_instance.spec.qualified_name
            )
        )

        time_spine_select_columns = []
        time_spine_dim_instances = []
        where_filter: Optional[SqlExpressionNode] = None

        # If offset_to_grain is used, will need to filter down to rows that match selected granularities.
        # Does not apply if one of the granularities selected matches the time spine column granularity.
        need_where_filter = (
            node.offset_to_grain
            and original_time_spine_dim_instance.spec not in node.requested_agg_time_dimension_specs
        )

        # Add requested granularities (if different from time_spine) and date_parts to time spine column.
        for time_dimension_instance in time_dimensions_to_select_from_time_spine:
            time_dimension_spec = time_dimension_instance.spec

            # TODO: this will break when we start supporting smaller grain than DAY unless the time spine table is
            # updated to use the smallest available grain.
            if (
                time_dimension_spec.time_granularity.to_int()
                < original_time_spine_dim_instance.spec.time_granularity.to_int()
            ):
                raise RuntimeError(
                    f"Can't join to time spine for a time dimension with a smaller granularity than that of the time "
                    f"spine column. Got {time_dimension_spec.time_granularity} for time dimension, "
                    f"{original_time_spine_dim_instance.spec.time_granularity} for time spine."
                )

            # Apply grain to time spine select expression, unless grain already matches original time spine column.
            select_expr: SqlExpressionNode = (
                time_spine_column_select_expr
                if time_dimension_spec.time_granularity == original_time_spine_dim_instance.spec.time_granularity
                else SqlDateTruncExpression(
                    time_granularity=time_dimension_spec.time_granularity, arg=time_spine_column_select_expr
                )
            )
            # Filter down to one row per granularity period requested in the group by. Any other granularities
            # included here will be filtered out in later nodes so should not be included in where filter.
            if need_where_filter and time_dimension_spec in node.requested_agg_time_dimension_specs:
                new_where_filter = SqlComparisonExpression(
                    left_expr=select_expr, comparison=SqlComparison.EQUALS, right_expr=time_spine_column_select_expr
                )
                where_filter = (
                    SqlLogicalExpression(operator=SqlLogicalOperator.OR, args=(where_filter, new_where_filter))
                    if where_filter
                    else new_where_filter
                )

            # Apply date_part to time spine column select expression.
            if time_dimension_spec.date_part:
                select_expr = SqlExtractExpression(date_part=time_dimension_spec.date_part, arg=select_expr)
            time_dim_spec = TimeDimensionSpec(
                element_name=original_time_spine_dim_instance.spec.element_name,
                entity_links=original_time_spine_dim_instance.spec.entity_links,
                time_granularity=time_dimension_spec.time_granularity,
                date_part=time_dimension_spec.date_part,
                aggregation_state=original_time_spine_dim_instance.spec.aggregation_state,
            )
            time_spine_dim_instance = TimeDimensionInstance(
                defined_from=original_time_spine_dim_instance.defined_from,
                associated_columns=(self._column_association_resolver.resolve_spec(time_dim_spec),),
                spec=time_dim_spec,
            )
            time_spine_dim_instances.append(time_spine_dim_instance)
            time_spine_select_columns.append(
                SqlSelectColumn(expr=select_expr, column_alias=time_spine_dim_instance.associated_column.column_name)
            )
        time_spine_instance_set = InstanceSet(time_dimension_instances=tuple(time_spine_dim_instances))

        return SqlDataSet(
            instance_set=InstanceSet.merge([time_spine_instance_set, parent_instance_set]),
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=tuple(time_spine_select_columns) + parent_select_columns,
                from_source=time_spine_dataset.checked_sql_select_node,
                from_source_alias=time_spine_alias,
                joins_descs=(join_description,),
                where=where_filter,
            ),
        )

    def visit_min_max_node(self, node: MinMaxNode) -> SqlDataSet:  # noqa: D102
        parent_data_set = node.parent_node.accept(self)
        parent_table_alias = self._next_unique_table_alias()
        assert (
            len(parent_data_set.checked_sql_select_node.select_columns) == 1
        ), "MinMaxNode supports exactly one parent select column."
        parent_column_alias = parent_data_set.checked_sql_select_node.select_columns[0].column_alias

        select_columns: List[SqlSelectColumn] = []
        metadata_instances: List[MetadataInstance] = []
        for agg_type in (AggregationType.MIN, AggregationType.MAX):
            metadata_spec = MetadataSpec.from_name(name=parent_column_alias, agg_type=agg_type)
            output_column_association = self._column_association_resolver.resolve_spec(metadata_spec)
            select_columns.append(
                SqlSelectColumn(
                    expr=SqlFunctionExpression.build_expression_from_aggregation_type(
                        aggregation_type=agg_type,
                        sql_column_expression=SqlColumnReferenceExpression(
                            SqlColumnReference(table_alias=parent_table_alias, column_name=parent_column_alias)
                        ),
                    ),
                    column_alias=output_column_association.column_name,
                )
            )
            metadata_instances.append(
                MetadataInstance(associated_columns=(output_column_association,), spec=metadata_spec)
            )

        return SqlDataSet(
            instance_set=parent_data_set.instance_set.transform(ConvertToMetadata(metadata_instances)),
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=tuple(select_columns),
                from_source=parent_data_set.checked_sql_select_node,
                from_source_alias=parent_table_alias,
            ),
        )

    def visit_add_generated_uuid_column_node(self, node: AddGeneratedUuidColumnNode) -> SqlDataSet:
        """Implements the behaviour of AddGeneratedUuidColumnNode.

        Builds a new dataset that is the same as the output dataset, but with an additional column
        that contains a randomly generated UUID.
        """
        input_data_set: SqlDataSet = node.parent_node.accept(self)
        input_data_set_alias = self._next_unique_table_alias()

        gen_uuid_spec = MetadataSpec.from_name(MetricFlowReservedKeywords.MF_INTERNAL_UUID.value)
        output_column_association = self._column_association_resolver.resolve_spec(gen_uuid_spec)
        output_instance_set = input_data_set.instance_set.transform(
            AddMetadata(
                (
                    MetadataInstance(
                        associated_columns=(output_column_association,),
                        spec=gen_uuid_spec,
                    ),
                )
            )
        )
        gen_uuid_sql_select_column = SqlSelectColumn(
            expr=SqlGenerateUuidExpression(), column_alias=output_column_association.column_name
        )

        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description="Add column with generated UUID",
                select_columns=input_data_set.instance_set.transform(
                    CreateSelectColumnsForInstances(input_data_set_alias, self._column_association_resolver)
                ).as_tuple()
                + (gen_uuid_sql_select_column,),
                from_source=input_data_set.checked_sql_select_node,
                from_source_alias=input_data_set_alias,
            ),
        )

    def visit_join_conversion_events_node(self, node: JoinConversionEventsNode) -> SqlDataSet:
        """Builds a resulting data set with all valid conversion events.

        This node takes the conversion and base data set and joins them against an entity and
        a valid time range to get successful conversions. It then deduplicates opportunities
        via the window function `first_value` to take the closest opportunity to the
        corresponding conversion. Then it returns a data set with each row representing a
        successful conversion. Duplication may exist in the result due to a single base event
        being able to link to multiple conversion events.
        """
        base_data_set: SqlDataSet = node.base_node.accept(self)
        base_data_set_alias = self._next_unique_table_alias()

        conversion_data_set: SqlDataSet = node.conversion_node.accept(self)
        conversion_data_set_alias = self._next_unique_table_alias()

        base_time_dimension_column_name = self._column_association_resolver.resolve_spec(
            node.base_time_dimension_spec
        ).column_name
        conversion_time_dimension_column_name = self._column_association_resolver.resolve_spec(
            node.conversion_time_dimension_spec
        ).column_name
        entity_column_name = self._column_association_resolver.resolve_spec(node.entity_spec).column_name

        constant_property_column_names: List[Tuple[str, str]] = []
        for constant_property in node.constant_properties or []:
            base_property_col_name = self._column_association_resolver.resolve_spec(
                constant_property.base_spec
            ).column_name
            conversion_property_col_name = self._column_association_resolver.resolve_spec(
                constant_property.conversion_spec
            ).column_name
            constant_property_column_names.append((base_property_col_name, conversion_property_col_name))

        # Builds the join conditions that is required for a successful conversion
        sql_join_description = SqlQueryPlanJoinBuilder.make_join_conversion_join_description(
            node=node,
            base_data_set=AnnotatedSqlDataSet(
                data_set=base_data_set,
                alias=base_data_set_alias,
                _metric_time_column_name=base_time_dimension_column_name,
            ),
            conversion_data_set=AnnotatedSqlDataSet(
                data_set=conversion_data_set,
                alias=conversion_data_set_alias,
                _metric_time_column_name=conversion_time_dimension_column_name,
            ),
            column_equality_descriptions=(
                ColumnEqualityDescription(
                    left_column_alias=entity_column_name,
                    right_column_alias=entity_column_name,
                ),
            )
            + tuple(
                ColumnEqualityDescription(left_column_alias=base_col, right_column_alias=conversion_col)
                for base_col, conversion_col in constant_property_column_names
            ),
        )

        # Builds the first_value window function columns
        base_sql_column_references = base_data_set.instance_set.transform(
            CreateSqlColumnReferencesForInstances(base_data_set_alias, self._column_association_resolver)
        )

        unique_conversion_col_names = tuple(
            self._column_association_resolver.resolve_spec(spec).column_name for spec in node.unique_identifier_keys
        )
        partition_by_columns: Tuple[str, ...] = (
            entity_column_name,
            conversion_time_dimension_column_name,
        ) + unique_conversion_col_names
        if node.constant_properties:
            partition_by_columns += tuple(
                conversion_column_name for _, conversion_column_name in constant_property_column_names
            )
        base_sql_select_columns = tuple(
            SqlSelectColumn(
                expr=SqlWindowFunctionExpression(
                    sql_function=SqlWindowFunction.FIRST_VALUE,
                    sql_function_args=[
                        SqlColumnReferenceExpression(
                            SqlColumnReference(
                                table_alias=base_data_set_alias,
                                column_name=base_sql_column_reference.col_ref.column_name,
                            ),
                        )
                    ],
                    partition_by_args=[
                        SqlColumnReferenceExpression(
                            SqlColumnReference(
                                table_alias=conversion_data_set_alias,
                                column_name=column,
                            ),
                        )
                        for column in partition_by_columns
                    ],
                    order_by_args=[
                        SqlWindowOrderByArgument(
                            expr=SqlColumnReferenceExpression(
                                SqlColumnReference(
                                    table_alias=base_data_set_alias,
                                    column_name=base_time_dimension_column_name,
                                ),
                            ),
                            descending=True,
                        )
                    ],
                ),
                column_alias=base_sql_column_reference.col_ref.column_name,
            )
            for base_sql_column_reference in base_sql_column_references
        )

        conversion_data_set_output_instance_set = conversion_data_set.instance_set.transform(
            FilterElements(include_specs=InstanceSpecSet(measure_specs=(node.conversion_measure_spec,)))
        )

        # Deduplicate the fanout results
        conversion_unique_key_select_columns = tuple(
            SqlSelectColumn(
                expr=SqlColumnReferenceExpression(
                    SqlColumnReference(
                        table_alias=conversion_data_set_alias,
                        column_name=column_name,
                    ),
                ),
                column_alias=column_name,
            )
            for column_name in unique_conversion_col_names
        )
        additional_conversion_select_columns = conversion_data_set_output_instance_set.transform(
            CreateSelectColumnsForInstances(conversion_data_set_alias, self._column_association_resolver)
        ).as_tuple()
        deduped_sql_select_node = SqlSelectStatementNode(
            description=f"Dedupe the fanout with {','.join(spec.qualified_name for spec in node.unique_identifier_keys)} in the conversion data set",
            select_columns=base_sql_select_columns
            + conversion_unique_key_select_columns
            + additional_conversion_select_columns,
            from_source=base_data_set.checked_sql_select_node,
            from_source_alias=base_data_set_alias,
            joins_descs=(sql_join_description,),
            distinct=True,
        )

        # Returns the original dataset with all the successful conversion
        output_data_set_alias = self._next_unique_table_alias()
        output_instance_set = ChangeAssociatedColumns(self._column_association_resolver).transform(
            InstanceSet.merge([conversion_data_set_output_instance_set, base_data_set.instance_set])
        )
        return SqlDataSet(
            instance_set=output_instance_set,
            sql_select_node=SqlSelectStatementNode(
                description=node.description,
                select_columns=output_instance_set.transform(
                    CreateSelectColumnsForInstances(output_data_set_alias, self._column_association_resolver)
                ).as_tuple(),
                from_source=deduped_sql_select_node,
                from_source_alias=output_data_set_alias,
            ),
        )
