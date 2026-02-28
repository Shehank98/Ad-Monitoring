# Role constants
SUPER_ADMIN = "super_admin"
ADMIN       = "admin"
TEAM_HEAD   = "team_head"
PLANNER     = "planner"
OPERATIONS  = "operations"

ROLE_LABELS = {
    SUPER_ADMIN: "Super Admin",
    ADMIN:       "Admin",
    TEAM_HEAD:   "Team Head",
    PLANNER:     "Planner",
    OPERATIONS:  "Operations",
}

# Higher number = more authority
ROLE_HIERARCHY = {
    SUPER_ADMIN: 5,
    ADMIN:       4,
    TEAM_HEAD:   3,
    PLANNER:     2,
    OPERATIONS:  2,
}

# Which roles each role is allowed to create
ROLE_CAN_CREATE = {
    SUPER_ADMIN: [ADMIN, TEAM_HEAD, PLANNER, OPERATIONS],
    ADMIN:       [TEAM_HEAD, PLANNER, OPERATIONS],
    TEAM_HEAD:   [],
    PLANNER:     [],
    OPERATIONS:  [],
}

# Sidebar pages available per role
ROLE_PAGES = {
    SUPER_ADMIN: ["Dashboard", "User Management", "Accounts", "System"],
    ADMIN:       ["Dashboard", "User Management"],
    TEAM_HEAD:   ["Dashboard", "Schedules", "Monitoring Data", "Verify Ads"],
    PLANNER:     ["Dashboard", "Upload Schedule", "My Schedules", "Verify Ads"],
    OPERATIONS:  ["Dashboard", "Upload Data", "Uploaded Data"],
}

# Page icons for sidebar
PAGE_ICONS = {
    "Dashboard":       "🏠",
    "User Management": "👥",
    "Accounts":        "🏢",
    "System":          "⚙️",
    "Schedules":       "📅",
    "Monitoring Data": "📊",
    "Verify Ads":      "✅",
    "Upload Schedule": "📤",
    "My Schedules":    "📋",
    "Upload Data":     "📤",
    "Uploaded Data":   "📊",
}
