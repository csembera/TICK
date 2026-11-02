# TICK — deploy the public pages that the publisher Lambda does not write.
#
#   make publish-static   sync site-static/ to the site bucket; invalidate
#
# The site bucket holds two kinds of page and only one of them is generated:
#
#   /            /docs  /demo      hand-authored marketing, this Makefile
#   /accounts  /<slug>/dashboard.html rendered nightly by the publisher Lambda
#
# They never collide — the publisher writes home/<key>/index.html and <slug>/ keys
# and nothing else, so it cannot overwrite these, and --delete is scoped to the
# two generated-free prefixes so this cannot delete the app.
#
# Bucket and distribution come from the deployed stack, so there is nothing to
# keep in sync by hand. Override with BUCKET=... DIST=... to target another.

STACK ?= tick-collector
REGION ?= us-east-1

.PHONY: publish-static

publish-static:
	@BUCKET="$(BUCKET)"; DIST="$(DIST)"; \
	if [ -z "$$BUCKET" ] || [ -z "$$DIST" ]; then \
	  echo "→ reading SiteBucketName / SiteDistributionId from stack $(STACK)"; \
	  OUT=$$(aws cloudformation describe-stacks --stack-name "$(STACK)" --region "$(REGION)" \
	         --query 'Stacks[0].Outputs' --output json) || \
	         { echo "could not read stack $(STACK) — is your AWS session current?"; exit 1; }; \
	  BUCKET=$$(echo "$$OUT" | jq -r '.[]|select(.OutputKey=="SiteBucketName")|.OutputValue'); \
	  DIST=$$(echo "$$OUT" | jq -r '.[]|select(.OutputKey=="SiteDistributionId")|.OutputValue'); \
	fi; \
	if [ -z "$$BUCKET" ] || [ "$$BUCKET" = "null" ]; then echo "SiteBucketName not found."; exit 1; fi; \
	if [ -z "$$DIST" ] || [ "$$DIST" = "null" ]; then echo "SiteDistributionId not found."; exit 1; fi; \
	echo "→ bucket $$BUCKET / distribution $$DIST"; \
	echo "→ aws s3 cp site-static/index.html s3://$$BUCKET/index.html"; \
	aws s3 cp site-static/index.html "s3://$$BUCKET/index.html" \
	  --content-type "text/html; charset=utf-8" \
	  --cache-control "no-cache, no-store, must-revalidate"; \
	echo "→ aws s3 cp site-static/og-image.png s3://$$BUCKET/og-image.png"; \
	aws s3 cp site-static/og-image.png "s3://$$BUCKET/og-image.png" \
	  --content-type "image/png" \
	  --cache-control "public, max-age=86400, must-revalidate"; \
	for p in docs demo; do \
	  echo "→ aws s3 sync site-static/$$p/ s3://$$BUCKET/$$p/"; \
	  aws s3 sync "site-static/$$p/" "s3://$$BUCKET/$$p/" \
	    --exclude "*" --include "*.html" --delete \
	    --content-type "text/html; charset=utf-8" \
	    --cache-control "no-cache, no-store, must-revalidate"; \
	  aws s3 sync "site-static/$$p/" "s3://$$BUCKET/$$p/" \
	    --exclude "*.html" --delete \
	    --cache-control "public, max-age=86400, must-revalidate" || true; \
	done; \
	echo "→ aws cloudfront create-invalidation --distribution-id $$DIST --paths / /index.html /og-image.png /docs/* /demo/*"; \
	aws cloudfront create-invalidation --distribution-id "$$DIST" \
	  --paths '/' '/index.html' '/og-image.png' '/docs/*' '/demo/*' > /dev/null; \
	echo "Done — invalidation typically propagates within a minute."

# ---------------------------------------------------------------- cognito UI
#
#   make publish-cognito-ui   apply the hosted-UI CSS *and* the banner logo
#   make cognito-css-default  dump Cognito's default CSS — the authoritative
#                             list of every class and property it will accept
#
# The hosted-UI customization is owned here, NOT by CloudFormation:
#
#   1. AWS::Cognito::UserPoolUICustomizationAttachment carries CSS only. The
#      banner is an uploaded IMAGE, so the wordmark cannot come from the
#      template at all.
#   2. SetUICustomization and that resource create the SAME underlying object.
#      Running this target and then `sam deploy` made CloudFormation fail with
#      AlreadyExists and roll the whole stack back. Exactly one of them may own
#      it, so the template no longer declares it.
#
# Safe to run at any point, before or after `sam deploy`, and as often as you
# like — it is idempotent. Cognito rejects the entire stylesheet if a single
# property is off its allowlist, so run it after editing cognito-ui.css.
.PHONY: publish-cognito-ui cognito-banner cognito-css-default

publish-cognito-ui:
	@POOL=$$(aws cloudformation describe-stacks --stack-name "$(STACK)" --region "$(REGION)" \
	  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text); \
	CLIENT=$$(aws cloudformation describe-stack-resource --stack-name "$(STACK)" \
	  --logical-resource-id SiteUserPoolClient --region "$(REGION)" \
	  --query 'StackResourceDetail.PhysicalResourceId' --output text) || \
	  { echo "could not resolve the app client — is the stack deployed and your session current?"; exit 1; }; \
	test -f cognito-ui.css || { echo "cognito-ui.css missing"; exit 1; }; \
	test -f site-static/branding/cognito-banner.png || \
	  { echo "site-static/branding/cognito-banner.png missing — run 'make cognito-banner'"; exit 1; }; \
	echo "→ pool $$POOL, client $$CLIENT"; \
	echo "→ $$(wc -l < cognito-ui.css | tr -d ' ') lines of CSS + $$(wc -c < site-static/branding/cognito-banner.png | tr -d ' ') byte banner"; \
	aws cognito-idp set-ui-customization --user-pool-id "$$POOL" --client-id "$$CLIENT" \
	  --css "$$(cat cognito-ui.css)" \
	  --image-file "fileb://site-static/branding/cognito-banner.png" \
	  --region "$(REGION)" > /dev/null && \
	  echo "applied — sign-in page restyled. Verify at the SiteLoginDomain stack output."

cognito-banner:
	@"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
	  --hide-scrollbars --default-background-color=00000000 --force-device-scale-factor=2 \
	  --window-size=420,56 --virtual-time-budget=8000 \
	  --screenshot=site-static/branding/cognito-banner.png \
	  "file://$$PWD/site-static/branding/cognito-banner.html" 2>/dev/null; \
	echo "rendered site-static/branding/cognito-banner.png ($$(wc -c < site-static/branding/cognito-banner.png | tr -d ' ') bytes; Cognito's limit is 100KB)"

cognito-css-default:
	@POOL=$$(aws cloudformation describe-stacks --stack-name "$(STACK)" --region "$(REGION)" \
	  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text); \
	aws cognito-idp get-ui-customization --user-pool-id "$$POOL" --client-id ALL \
	  --region "$(REGION)" --query 'UICustomization.CSS' --output text

# ── waitlist ────────────────────────────────────────────────────────────────
#   make list-requests   every /api/join request, oldest first
.PHONY: list-requests list-account-requests

list-requests:
	@aws dynamodb scan --table-name "$(STACK)-join-requests" --region "$(REGION)" --output json \
	  | jq -r '.Items | sort_by(.created_at.S)[] | [.created_at.S, .status.S, .email.S, .name.S, .company.S] | @tsv' \
	  | column -t -s "$$(printf '\t')"

list-account-requests:
	@aws dynamodb scan --table-name "$(STACK)-account-requests" --region "$(REGION)" --output json \
	  | jq -r '.Items | sort_by(.created_at.S)[] | [.created_at.S, .status.S, (.requester_email.S // .requester_sub.S), .company.S, (.careers_url.S // "-")] | @tsv' \
	  | column -t -s "$$(printf '\t')"
