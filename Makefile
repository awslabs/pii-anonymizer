.PHONY: cfn-deploy cfn-update cfn-delete tf-init tf-plan tf-deploy tf-destroy layer frontend-env-cfn frontend-env-tf

# ============================================================
# CloudFormation (SAM)
# ============================================================
cfn-deploy:
	./infra/cfn/deploy.sh deploy

cfn-update:
	./infra/cfn/deploy.sh deploy

cfn-delete:
	./infra/cfn/deploy.sh delete

# ============================================================
# Terraform
# ============================================================
tf-init:
	cd infra/terraform && terraform init

tf-plan: layer
	cd infra/terraform && terraform plan

tf-deploy: layer
	cd infra/terraform && terraform apply -auto-approve

tf-destroy:
	cd infra/terraform && terraform destroy -auto-approve

# ============================================================
# Lambda Layer
# ============================================================
layer:
	@if [ ! -d "layers/lambda_layer/python" ] || [ -z "$$(ls -A layers/lambda_layer/python 2>/dev/null)" ] || [ ! -f "layers/ffmpeg_layer/bin/ffmpeg" ] || [ requirements_lambda.txt -nt layers/lambda_layer/python ]; then \
		echo "Building Lambda layers (missing or requirements_lambda.txt changed)..."; \
		chmod +x create_layer.sh && ./create_layer.sh; \
	else \
		echo "Lambda layers up to date (delete layers/lambda_layer/python to force rebuild)."; \
	fi

# ============================================================
# Frontend Configuration
# ============================================================
frontend-env-cfn:
	chmod +x generate_frontend_env.sh && ./generate_frontend_env.sh cfn

frontend-env-tf:
	chmod +x generate_frontend_env.sh && ./generate_frontend_env.sh terraform
