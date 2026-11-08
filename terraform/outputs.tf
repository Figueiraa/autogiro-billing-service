output "project_id" {
  description = "ID do projeto no Neon."
  value       = neon_project.billing.id
}

# A aplicação usa SQLAlchemy assíncrono (driver asyncpg).
output "database_url" {
  description = "DATABASE_URL do Billing Service (asyncpg)."
  value = format(
    "postgresql+asyncpg://%s:%s@%s/%s?ssl=require",
    var.app_role,
    data.neon_branch_role_password.app.password,
    neon_project.billing.database_host,
    var.database_name,
  )
  sensitive = true
}
