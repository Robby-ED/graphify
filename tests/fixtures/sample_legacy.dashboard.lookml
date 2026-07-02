---
- dashboard: orders_overview_legacy
  title: Orders Overview (Legacy)
  layout: newspaper
  preferred_viewer: dashboards-next
  filters: []
  elements:
  - name: banner
    type: text
    title_text: ''
    body_text: Orders dashboard
  - name: orders_by_status
    type: vis
    model: ecommerce
    explore: orders
    fields: [orders.status, orders.count]
