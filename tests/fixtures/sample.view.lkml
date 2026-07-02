view: orders {
  sql_table_name: analytics.orders ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: status {
    type: string
    sql: ${TABLE}.status ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, week, month]
    sql: ${TABLE}.created_at ;;
  }

  measure: count {
    type: count
  }

  measure: completed_count {
    type: count
    filters: [status: "completed"]
  }

  measure: completion_rate {
    type: number
    sql: 1.0 * ${completed_count} / nullif(${count}, 0) ;;
  }

  set: detail {
    fields: [id, status, created_date]
  }
}

view: orders_summary {
  extends: [orders]

  derived_table: {
    sql:
      with recent as (
        select * from orders where created_at > dateadd('day', -30, current_date)
      )
      select customer_id, count(*) as order_count
      from recent
      join customers on recent.customer_id = customers.id
      group by 1 ;;
  }

  dimension: customer_id {
    type: number
    sql: ${TABLE}.customer_id ;;
  }

  measure: order_count {
    type: sum
    sql: ${TABLE}.order_count ;;
  }
}
