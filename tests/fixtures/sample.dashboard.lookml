dashboard: orders_overview {
  title: "Orders Overview"
  layout: newspaper

  element: {
    name: orders_by_status
    type: looker_grid
    model: ecommerce
    explore: orders
    fields: [orders.status, orders.count]
    sorts: [orders.count desc]
    row: 0
    col: 0
    width: 12
    height: 6
  }
}
