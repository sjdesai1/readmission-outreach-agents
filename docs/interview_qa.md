# Interview Q&A

One section per milestone. Answers written the way to say them.

## Milestone 0: Local stack

**Q: Walk me through your local setup.**
Docker Compose runs the full stack: Postgres, Airflow, MLflow, a HAPI FHIR server as the simulated EHR, and Mailpit to catch outgoing email. Gemini and LangSmith are the two cloud pieces. One command brings it up, and a smoke test checks each service from the laptop and from inside Airflow.

**Q: Why a FHIR server instead of CSV files?**
FHIR is how EHRs like Epic expose data. Reading patients, encounters and notes as FHIR resources, and booking FHIR Appointments, means the integration code looks like it would in a real hospital. Swapping the simulated server for a real FHIR endpoint changes a URL, not the code.

**Q: Airflow and your ML libraries have conflicting dependencies. How did you handle it?**
Airflow only orchestrates. Project code runs in a separate virtual environment inside the image, and tasks call that Python directly. Airflow's pinned versions never touch XGBoost or LangGraph. In production the same idea is one container per task, for example with the KubernetesPodOperator.

**Q: How does the same code run on your laptop and inside Docker?**
All settings come from environment variables. On the laptop, `.env` points at localhost ports. Inside Docker, compose overrides them with service names. The config module reads whatever is set, so there's no environment-specific code.

**Q: How do you handle secrets?**
Keys only live in `.env`, which is gitignored. `.env.example` is committed with every variable and blank secrets. A unit test fails if the code reads a variable that isn't documented there. In production they'd come from a secrets manager like Vault or AWS Secrets Manager.

**Q: Why Postgres for both Airflow and the app?**
One server, separate databases: Airflow metadata, MLflow tracking, and app data. Simple locally, and isolated enough that a reset of one doesn't affect the others. In production these would usually be separate managed instances.

**Q: What does MLflow store where?**
Run metadata, params, metrics and the model registry go to Postgres. Model files and plots go to the artifact store, here a Docker volume, in production S3 or similar.

**Q: What does your smoke test check, and why bother?**
Each service independently: Postgres query, FHIR write/read/delete, an MLflow run with an artifact, an email sent and found in Mailpit, Airflow's health endpoint, Gemini model listing and a call through LiteLLM with fallback, and a LangSmith trace. It runs from the laptop and as an Airflow DAG. It catches broken wiring before any real pipeline runs, and it's the first thing I run after any infra change.

**Q: What would change for production?**
Managed Airflow with components split out, managed Postgres, S3 for artifacts, a BAA-covered LLM provider behind the same LiteLLM interface, secrets from a vault, LangSmith self-hosted or with PHI redaction, and Kubernetes instead of Compose.
