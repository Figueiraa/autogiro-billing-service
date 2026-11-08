# Banco próprio do Billing Service: um projeto Neon só dele, com credenciais que
# nenhum outro serviço recebe.
resource "neon_project" "billing" {
  name                      = var.project_name
  org_id                    = var.org_id
  region_id                 = var.region
  pg_version                = var.pg_version
  history_retention_seconds = var.history_retention_seconds

  branch {
    name          = "main"
    database_name = var.database_name
    role_name     = var.app_role
  }
}

data "neon_branch_role_password" "app" {
  project_id = neon_project.billing.id
  branch_id  = neon_project.billing.default_branch_id
  role_name  = var.app_role
}
