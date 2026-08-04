// Extend Vue Router's type definitions
import 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    // Whether login is required to access this route
    requiresAuth?: boolean
    // Whether this route renders an admin-only task replay.
    adminReplay?: boolean
  }
}
