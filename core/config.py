WP_CONTENT_ROOT = "/home/username/public_html/wp-content"
WP_DB_PREFIX = "wpxe_"
UPLOAD_LOCATIONS = [
    "uploads",
    "uploads-webpc/uploads"
]

USER_REFERENCE_COLUMNS = {
    "user_id",
    "post_author",
    "comment_user_id",
    "created_by",
    "author_id",
    "owner_id",
    "client",          # ✅ your WP uses this meta key value as user id
    "customer_id",     # ✅ WooCommerce / custom
    "assigned_user",   # ✅ optional custom
}
