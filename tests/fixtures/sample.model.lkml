connection: "ecommerce_warehouse"

include: "*.view.lkml"

explore: orders {
}

explore: orders_summary {
  join: customers {
    type: left_outer
    sql_on: ${orders_summary.customer_id} = ${customers.id} ;;
    relationship: many_to_one
  }
}
