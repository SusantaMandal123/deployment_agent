from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from deployment_service import (
    Credentials,
    DeploymentError,
    RepositoryNameTaken,
    VercelBuildFailure,
    create_repository,
    create_vercel_project,
    find_vercel_incompatibility,
    analyze_with_groq,
    repair_known_vercel_failure,
    upload_project,
    wait_for_vercel_deployment,
)

load_dotenv()
st.set_page_config(page_title="GitHub → Vercel Deploy Agent", page_icon="🚀", layout="centered")

if "stage" not in st.session_state:
    st.session_state.stage = "credentials"
if "repository" not in st.session_state:
    st.session_state.repository = None

st.title("🚀 Deployment Agent")
st.caption("Upload a local project to GitHub, then deploy its main branch through Vercel.")

st.progress({"credentials": 5, "folder": 15, "repository": 35, "vercel_project": 55, "upload": 75, "vercel": 90, "done": 100}[st.session_state.stage])

if st.session_state.stage == "credentials":
    st.subheader("1. Connect your accounts")
    st.caption("Credentials are kept only in this browser session and are never written to disk.")
    with st.form("credentials_form"):
        github_username = st.text_input("GitHub username")
        github_token = st.text_input("GitHub Personal Access Token", type="password")
        vercel_username = st.text_input("Vercel username")
        vercel_token = st.text_input("Vercel token", type="password")
        vercel_team_id = st.text_input("Vercel team ID (optional)")
        submitted = st.form_submit_button("Continue")
    if submitted:
        if not all((github_username.strip(), github_token.strip(), vercel_username.strip(), vercel_token.strip())):
            st.error("GitHub and Vercel usernames and tokens are required.")
        else:
            st.session_state.credentials = Credentials(
                github_username=github_username.strip(),
                github_token=github_token.strip(),
                vercel_username=vercel_username.strip(),
                vercel_token=vercel_token.strip(),
                vercel_team_id=vercel_team_id.strip(),
                groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
                groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b",
            )
            st.session_state.stage = "folder"
            st.rerun()

elif st.session_state.stage == "folder":
    st.subheader("2. Choose the project folder")
    with st.form("folder_form"):
        folder = st.text_input("Full path to the project folder", placeholder=r"C:\Projects\my-app")
        submitted = st.form_submit_button("Use this project")
    if submitted:
        if Path(folder).expanduser().is_dir():
            resolved_folder = str(Path(folder).expanduser().resolve())
            incompatibility = find_vercel_incompatibility(resolved_folder)
            if incompatibility:
                project_type, explanation = incompatibility
                st.warning(
                    f"This is a {project_type}. This type of app cannot be deployed directly to Vercel. {explanation}"
                )
            else:
                st.session_state.folder = resolved_folder
                st.session_state.stage = "repository"
                st.rerun()
        else:
            st.error("Enter an existing project folder.")

elif st.session_state.stage == "repository":
    st.subheader("3. Provide a GitHub repository name")
    st.caption(f"Project: `{st.session_state.folder}`")
    with st.form("repository_form"):
        name = st.text_input("Repository name", placeholder="my-production-app")
        visibility = st.radio("Repository visibility", ("Private", "Public"), horizontal=True)
        submitted = st.form_submit_button("Create GitHub repository")
    if submitted:
        if not name.strip():
            st.error("A repository name is required.")
        else:
            try:
                with st.spinner("Creating a private GitHub repository..."):
                    st.session_state.repository = create_repository(
                        name.strip(), st.session_state.credentials, private=visibility == "Private"
                    )
                st.session_state.stage = "vercel_project"
                st.success(f"{visibility} GitHub repository created.")
                st.rerun()
            except RepositoryNameTaken as error:
                st.error(str(error))
            except DeploymentError as error:
                st.error(str(error))

elif st.session_state.stage == "vercel_project":
    st.subheader("4. Create and link the Vercel project")
    st.caption("The project must be linked before `main` is pushed, so Vercel deploys from GitHub.")
    st.info("One-time prerequisite: authorize the Vercel GitHub integration in Vercel Account Settings → Git Integrations.")
    if st.button("Create Vercel project", type="primary"):
        try:
            with st.status("Creating Vercel project linked to GitHub...", expanded=True) as status:
                st.session_state.vercel_project = create_vercel_project(st.session_state.repository, st.session_state.credentials)
                status.update(label="Vercel project created and linked", state="complete")
            st.session_state.stage = "upload"
            st.rerun()
        except DeploymentError as error:
            st.error(str(error))

elif st.session_state.stage == "upload":
    st.subheader("5. Upload project to GitHub")
    st.success(f"Repository created: {st.session_state.repository['html_url']}")
    if st.button("Upload to GitHub", type="primary"):
        try:
            with st.status("Uploading project to GitHub...", expanded=True) as status:
                st.write("Initializing Git and committing project files on `main`.")
                upload_project(st.session_state.folder, st.session_state.repository, st.session_state.credentials)
                status.update(label="Uploaded to GitHub", state="complete")
            st.session_state.stage = "vercel"
            st.rerun()
        except DeploymentError as error:
            st.error(str(error))

elif st.session_state.stage == "vercel":
    st.subheader("6. Vercel is deploying main from GitHub")
    st.info("The main-branch push triggered the linked Vercel project.")
    if st.button("Check deployment status", type="primary"):
        try:
            with st.status("Deploying main to Vercel...", expanded=True) as status:
                st.write("Waiting for Vercel to finish building.")
                try:
                    st.session_state.deployment_url = wait_for_vercel_deployment(st.session_state.vercel_project, st.session_state.credentials)
                except VercelBuildFailure as failure:
                    st.write("Build failed. Diagnosing the Vercel build log.")
                    repair = repair_known_vercel_failure(st.session_state.folder, failure, st.session_state.credentials)
                    if not repair:
                        analysis = analyze_with_groq(st.session_state.folder, failure, st.session_state.credentials)
                        if analysis:
                            st.session_state.ai_diagnosis = analysis
                            raise DeploymentError("Vercel could not deploy this project. An AI diagnosis is shown below.")
                        raise DeploymentError(f"Vercel could not deploy this project: {failure}")
                    st.write(repair)
                    st.write("Waiting for Vercel to deploy the repaired main branch.")
                    st.session_state.deployment_url = wait_for_vercel_deployment(
                        st.session_state.vercel_project,
                        st.session_state.credentials,
                        after_uid=failure.deployment.get("uid") or failure.deployment.get("id"),
                    )
                status.update(label="Deployed to Vercel", state="complete")
            st.session_state.stage = "done"
            st.rerun()
        except DeploymentError as error:
            st.error(str(error))
            if st.session_state.get("ai_diagnosis"):
                with st.expander("AI deployment diagnosis", expanded=True):
                    st.write(st.session_state.ai_diagnosis)

else:
    st.balloons()
    st.success("Production deployment completed.")
    st.link_button("Open deployed project", st.session_state.deployment_url, type="primary")
    st.code(st.session_state.deployment_url)
    if st.button("Deploy another project"):
        for key in ("stage", "repository", "folder", "vercel_project", "deployment_url", "ai_diagnosis", "credentials"):
            st.session_state.pop(key, None)
        st.rerun()