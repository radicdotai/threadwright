#!/usr/bin/env bash
set -euo pipefail

SITE="$(dirname "$0")/site.env"
[[ -f "$SITE" ]] && source "$SITE"
PROJECT="${PROJECT:?set PROJECT}"
ZONE="${ZONE:-us-central1-a}"
READ_PROJECTS="${READ_PROJECTS:-$PROJECT}"
DB_INSTANCES="${DB_INSTANCES:-}"
ACTION_KEYRING="${ACTION_KEYRING:-}"
ACTION_LOCATION="${ACTION_LOCATION:-us-central1}"
WORKER_TYPE="${WORKER_TYPE:-e2-standard-8}"
DRY_RUN="${DRY_RUN:-0}"

run() {
  echo "+ $*"
  [[ "$DRY_RUN" == 1 ]] && return 0
  "$@" || echo "  ^ failed (fine if it already exists; otherwise fix and re-run)" >&2
}

LISTENER_SA="threadwright-listener@$PROJECT.iam.gserviceaccount.com"
WORKER_SA="threadwright-worker@$PROJECT.iam.gserviceaccount.com"
IMAGE=(--image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --shielded-secure-boot)

run gcloud --project "$PROJECT" iam service-accounts create threadwright-listener --display-name "Threadwright listener"
run gcloud --project "$PROJECT" iam service-accounts create threadwright-worker --display-name "Threadwright worker"

run gcloud --project "$PROJECT" pubsub topics create threadwright-tasks threadwright-updates
run gcloud --project "$PROJECT" pubsub subscriptions create threadwright-tasks-worker --topic threadwright-tasks \
  --ack-deadline 600 --message-retention-duration 1d --expiration-period never
run gcloud --project "$PROJECT" pubsub subscriptions create threadwright-updates-listener --topic threadwright-updates \
  --ack-deadline 60 --message-retention-duration 1d --expiration-period never

run gcloud --project "$PROJECT" pubsub topics add-iam-policy-binding threadwright-tasks \
  --member "serviceAccount:$LISTENER_SA" --role roles/pubsub.publisher
run gcloud --project "$PROJECT" pubsub subscriptions add-iam-policy-binding threadwright-updates-listener \
  --member "serviceAccount:$LISTENER_SA" --role roles/pubsub.subscriber
run gcloud --project "$PROJECT" pubsub subscriptions add-iam-policy-binding threadwright-tasks-worker \
  --member "serviceAccount:$WORKER_SA" --role roles/pubsub.subscriber
run gcloud --project "$PROJECT" pubsub topics add-iam-policy-binding threadwright-updates \
  --member "serviceAccount:$WORKER_SA" --role roles/pubsub.publisher

for role in roles/logging.viewer roles/run.viewer roles/monitoring.viewer; do
  for target in $READ_PROJECTS; do
    run gcloud projects add-iam-policy-binding "$target" --member "serviceAccount:$WORKER_SA" --role "$role" \
      --condition None
  done
done

run gcloud iam roles create threadwrightWorkerPower --project "$PROJECT" --title "Threadwright worker power" \
  --permissions compute.instances.get,compute.instances.start,compute.instances.stop,compute.instances.suspend,compute.instances.resume
run gcloud iam roles create threadwrightZoneOps --project "$PROJECT" --title "Threadwright operation status" \
  --permissions compute.zoneOperations.get

run gcloud --project "$PROJECT" compute instances create threadwright-worker --zone "$ZONE" \
  --machine-type "$WORKER_TYPE" "${IMAGE[@]}" --boot-disk-size 100GB --boot-disk-type pd-balanced \
  --service-account "$WORKER_SA" --scopes cloud-platform
run gcloud --project "$PROJECT" compute instances create threadwright-listener --zone "$ZONE" \
  --machine-type e2-micro "${IMAGE[@]}" --boot-disk-size 30GB --boot-disk-type pd-standard \
  --service-account "$LISTENER_SA" --scopes cloud-platform

run gcloud --project "$PROJECT" compute instances add-iam-policy-binding threadwright-worker --zone "$ZONE" \
  --member "serviceAccount:$LISTENER_SA" --role "projects/$PROJECT/roles/threadwrightWorkerPower"

run gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$LISTENER_SA" \
  --role "projects/$PROJECT/roles/threadwrightZoneOps" --condition None
run gcloud --project "$PROJECT" iam service-accounts add-iam-policy-binding "$WORKER_SA" \
  --member "serviceAccount:$LISTENER_SA" --role roles/iam.serviceAccountUser

if [[ -n "$DB_INSTANCES" ]]; then
  cond=""
  for inst in $DB_INSTANCES; do
    cond+="${cond:+ || }resource.name == 'projects/$PROJECT/instances/$inst'"
  done
  for role in roles/cloudsql.client roles/cloudsql.instanceUser; do
    run gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$WORKER_SA" --role "$role" \
      --condition "expression=$cond,title=threadwright-db-read"
  done
  for inst in $DB_INSTANCES; do
    run gcloud --project "$PROJECT" sql users create "threadwright-worker@$PROJECT.iam" --instance "$inst" \
      --type cloud_iam_service_account
  done
  echo "Then run deploy/db-readonly-grants.sql on each database (README)."
fi

if [[ -n "$ACTION_KEYRING" ]]; then
  ACTION_BACKEND_SA="${ACTION_BACKEND_SA:?set ACTION_BACKEND_SA, the service account your API verifies keys with}"
  run gcloud --project "$PROJECT" kms keys create threadwright-assertion --keyring "$ACTION_KEYRING" \
    --location "$ACTION_LOCATION" --purpose asymmetric-signing --default-algorithm ec-sign-p256-sha256 \
    --protection-level software
  run gcloud --project "$PROJECT" kms keys add-iam-policy-binding threadwright-assertion --keyring "$ACTION_KEYRING" \
    --location "$ACTION_LOCATION" --member "serviceAccount:$ACTION_BACKEND_SA" --role roles/cloudkms.publicKeyViewer
  run gcloud --project "$PROJECT" kms keys add-iam-policy-binding threadwright-assertion --keyring "$ACTION_KEYRING" \
    --location "$ACTION_LOCATION" --member "serviceAccount:$LISTENER_SA" --role roles/cloudkms.signer
fi

echo "Done. Next: copy this repo to both VMs and run deploy/bootstrap.sh listener|worker (README)."
