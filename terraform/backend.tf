# State no HCP Terraform (plano gratuito). `neon_project` não é idempotente por
# nome: um state perdido faria o próximo `apply` criar um segundo projeto no Neon.
#
# Autenticação: `TF_TOKEN_app_terraform_io` no ambiente ou `terraform login`.
terraform {
  cloud {
    organization = "autogiro"

    workspaces {
      name = "autogiro-billing-service"
    }
  }
}
