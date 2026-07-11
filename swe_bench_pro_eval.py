"""
The script is used to evaluate the performance of the SWEAP Pro agent with Modal.

This evaluation script:
1. Takes a CSV file containing test cases and a JSON file containing patches
2. Runs each patch in a Modal sandbox environment using Docker Hub images
3. Executes the tests using local run scripts and collects results
4. Calculates overall accuracy based on test pass/fail status

Usage:
python sweap_pro_eval_modal.py \
    --raw_sample_path=data.csv \
    --patch_path={OUTPUT}/gold_patches.json \
    --output_dir={OUTPUT}/ \
    --scripts_dir=run_scripts \
    --num_workers=100 \
    --dockerhub_username=your-username

It expects:
- Local run scripts in run_scripts/{instance_id}/run_script.sh
- Local parser scripts in run_scripts/{instance_id}/parser.py
- CSV file with columns: instance_id, before_repo_set_cmd, selected_test_files_to_run, 
  base_commit, base_dockerfile, instance_dockerfile, FAIL_TO_PASS, PASS_TO_PASS

And the generated patch file (gold_patches.json) should have the following format:
[
    {
        "instance_id": "unique_id",
        "patch": "git patch content",
        "prefix": "optional_prefix"
    },
    ...
]
"""

import argparse
import concurrent.futures
import json
import os
import platform as py_platform
import re

try:
    import modal  # Lazy/optional: only required when not using --use_local_docker
except Exception:
    modal = None
try:
    import docker  # Optional: used when --use_local_docker is set
except Exception:
    docker = None
import pandas as pd
from tqdm import tqdm

from helper_code.image_uri import get_dockerhub_image_uri


TIMING_SENSITIVE_WORKERS = 4

# These instances either exhibited load-sensitive timeouts in repeated local
# evaluations or contain unusually tight timing assertions. Keep the list
# precise so unrelated instances still use the requested concurrency.
TIMING_SENSITIVE_INSTANCE_IDS = frozenset(
    {
        # Tutanota instances whose selected tests include RestClientTest.js.
        "instance_tutao__tutanota-da4edb7375c10f47f4ed3860a591c5e6557f7b5c-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-09c2776c0fce3db5c6e18da92b5a45dce9f013aa-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-1e516e989b3c0221f4af6b297d9c0e4c43e4adc3-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-f3ffe17af6e8ab007e8d461355057ad237846d9d-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-fbdb72a2bd39b05131ff905780d9d4a2a074de26-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-fb32e5f9d9fc152a00144d56dd0af01760a2d4dc-vc4e41fd0029957297843cb9dec4a25c7c756f029",
        "instance_tutao__tutanota-b4934a0f3c34d9d7649e944b183137e8fad3e859-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-fe240cbf7f0fdd6744ef7bef8cb61676bcdbb621-vc4e41fd0029957297843cb9dec4a25c7c756f029",
        "instance_tutao__tutanota-40e94dee2bcec2b63f362da283123e9df1874cc1-vc4e41fd0029957297843cb9dec4a25c7c756f029",
        "instance_tutao__tutanota-befce4b146002b9abc86aa95f4d57581771815ce-vee878bb72091875e912c52fc32bc60ec3760227b",
        "instance_tutao__tutanota-12a6cbaa4f8b43c2f85caca0787ab55501539955-vc4e41fd0029957297843cb9dec4a25c7c756f029",
        "instance_tutao__tutanota-db90ac26ab78addf72a8efaff3c7acc0fbd6d000-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-219bc8f05d7b980e038bc1524cb021bf56397a1b-vee878bb72091875e912c52fc32bc60ec3760227b",
        "instance_tutao__tutanota-8513a9e8114a8b42e64f4348335e0f23efa054c4-vee878bb72091875e912c52fc32bc60ec3760227b",
        "instance_tutao__tutanota-1ff82aa365763cee2d609c9d19360ad87fdf2ec7-vc4e41fd0029957297843cb9dec4a25c7c756f029",
        "instance_tutao__tutanota-d1aa0ecec288bfc800cfb9133b087c4f81ad8b38-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
        "instance_tutao__tutanota-4b4e45949096bb288f2b522f657610e480efa3e8-vee878bb72091875e912c52fc32bc60ec3760227b",
        # Other observed or statically identified timing-sensitive instances.
        "instance_NodeBB__NodeBB-51d8f3b195bddb13a13ddc0de110722774d9bb1b-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e",
        "instance_ansible__ansible-40ade1f84b8bb10a63576b0ac320c13f57c87d34-v6382ea168a93d80a64aab1fbd8c4f02dc5ada5bf",
        "instance_element-hq__element-web-fe14847bb9bb07cab1b9c6c54335ff22ca5e516a-vnan",
        "instance_gravitational__teleport-0ecf31de0e98b272a6a2610abe1bbedd379a38a3-vce94f93ad1030e3136852817f2423c1b3ac37bc4",
        "instance_gravitational__teleport-1316e6728a3ee2fc124e2ea0cc6a02044c87a144-v626ec2a48416b10a88641359a169d99e935ff037",
        "instance_gravitational__teleport-4f771403dc4177dc26ee0370f7332f3fe54bee0f-vee9b09fb20c43af7e520f57e9239bbcf46b7113d",
        "instance_gravitational__teleport-629dc432eb191ca479588a8c49205debb83e80e2",
        "instance_gravitational__teleport-78b0d8c72637df1129fb6ff84fc49ef4b5ab1288",
        "instance_gravitational__teleport-bb562408da4adeae16e025be65e170959d1ec492-vee9b09fb20c43af7e520f57e9239bbcf46b7113d",
        "instance_gravitational__teleport-e6681abe6a7113cfd2da507f05581b7bdf398540-v626ec2a48416b10a88641359a169d99e935ff037",
        "instance_gravitational__teleport-e6d86299a855687b21970504fbf06f52a8f80c74-vce94f93ad1030e3136852817f2423c1b3ac37bc4",
        "instance_navidrome__navidrome-29b7b740ce469201af0a0510f3024adc93ef4c8e",
        "instance_navidrome__navidrome-29bc17acd71596ae92131aca728716baf5af9906",
        "instance_navidrome__navidrome-3972616585e82305eaf26aa25697b3f5f3082288",
        "instance_navidrome__navidrome-3bc9e75b2843f91f6a1e9b604e321c2bd4fd442a",
    }
)


def plan_evaluation_phases(patches, requested_workers):
    """Split timing-sensitive instances into a quiet, four-worker phase."""
    if requested_workers < 1:
        raise ValueError("--num_workers must be at least 1")

    if requested_workers < TIMING_SENSITIVE_WORKERS:
        return [("all instances", patches, requested_workers)]

    regular_patches = []
    timing_sensitive_patches = []
    for patch in patches:
        if patch["instance_id"] in TIMING_SENSITIVE_INSTANCE_IDS:
            timing_sensitive_patches.append(patch)
        else:
            regular_patches.append(patch)

    phases = []
    if regular_patches:
        phases.append(("regular instances", regular_patches, requested_workers))
    if timing_sensitive_patches:
        phases.append(
            (
                "timing-sensitive instances",
                timing_sensitive_patches,
                TIMING_SENSITIVE_WORKERS,
            )
        )
    return phases


# Credit: prabhuteja12
def load_base_docker(iid):
    with open(f"dockerfiles/base_dockerfile/{iid}/Dockerfile") as fp:
        return fp.read()

def instance_docker(iid):
    with open(f"dockerfiles/instance_dockerfile/{iid}/Dockerfile") as fp:
        return fp.read()

def load_local_script(scripts_dir, instance_id, script_name):
    """Load a script file from local scripts directory."""
    script_path = os.path.join(scripts_dir, instance_id, script_name)
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"Script not found: {script_path}")
    
    with open(script_path, 'r') as f:
        return f.read()


def strip_binary_hunks(patch: str) -> str:
    """Remove binary diff sections from a git patch."""
    if not patch:
        return patch

    sections = re.split(r'(?=^diff --git )', patch, flags=re.MULTILINE)

    kept: list[str] = []
    for section in sections:
        if not section.strip():
            continue
        if re.search(r'^Binary files .* differ$', section, re.MULTILINE):
            continue
        if re.search(r'^GIT binary patch$', section, re.MULTILINE):
            continue
        kept.append(section)

    return "".join(kept)


def create_entryscript(sample):
    before_repo_set_cmd = sample["before_repo_set_cmd"].strip().split("\n")[-1]
    selected_test_files_to_run = ",".join(eval(sample["selected_test_files_to_run"]))
    base_commit = sample["base_commit"]
    base_dockerfile = load_base_docker(sample["instance_id"])
    instance_dockerfile = instance_docker(sample["instance_id"])
    
    # Extract ENV commands from dockerfiles
    env_cmds = []
    for dockerfile_content in [base_dockerfile, instance_dockerfile]:
        for line in dockerfile_content.split("\n"):
            line = line.strip()
            if line.startswith("ENV"):
                # Convert ENV commands to export statements
                env_cmd = line.replace("ENV", "export", 1)
                env_cmds.append(env_cmd)
    
    env_cmds = "\n".join(env_cmds)

    entry_script = f"""
{env_cmds}
# apply patch
cd /app
PATCH_APPLY_STATUS=/workspace/patch_apply_status.json
PATCH_APPLY_STDERR=/workspace/patch_apply.stderr
if ! git reset --hard {base_commit}; then
  printf '%s\n' '{{"success": false, "stage": "reset"}}' > "$PATCH_APPLY_STATUS"
  exit 80
fi
if ! git checkout {base_commit}; then
  printf '%s\n' '{{"success": false, "stage": "checkout"}}' > "$PATCH_APPLY_STATUS"
  exit 81
fi
if ! git apply --check /workspace/patch.diff 2> "$PATCH_APPLY_STDERR"; then
  printf '%s\n' '{{"success": false, "stage": "check"}}' > "$PATCH_APPLY_STATUS"
  exit 82
fi
if ! git apply -v /workspace/patch.diff 2> "$PATCH_APPLY_STDERR"; then
  printf '%s\n' '{{"success": false, "stage": "apply"}}' > "$PATCH_APPLY_STATUS"
  exit 83
fi
printf '%s\n' '{{"success": true, "stage": "applied"}}' > "$PATCH_APPLY_STATUS"
{before_repo_set_cmd}
# run test and save stdout and stderr to separate files
bash /workspace/run_script.sh {selected_test_files_to_run} > /workspace/stdout.log 2> /workspace/stderr.log
# run parsing script
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
"""
    return entry_script


def create_dockerhub_tag(uid, repo_name=""):
    """
    Convert instance_id and repo name to Docker Hub compatible tag format.
    This must match the format used in the upload script.

    Args:
        uid (str): The instance_id (e.g., "django__django-12345")
        repo_name (str): The repository name from ECR (e.g., "sweap-images/nodebb.nodebb")

    Returns:
        str: Docker Hub compatible tag (e.g., "nodebb-nodebb-12345")
    """
    if repo_name:
        # For "NodeBB/NodeBB" -> repo_base="nodebb", repo_name="nodebb" 
        # Format: {repo_base}.{repo_name}-{OriginalCase}__{OriginalCase}-{hash}-{version}
        # Example: nodebb.nodebb-NodeBB__NodeBB-7b8bffd763e2155cf88f3ebc258fa68ebe18188d-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e
        repo_base, repo_name_only = repo_name.lower().split("/")
        # Keep original case for the instance_id part (after removing "instance_" prefix)
        hsh = uid.replace("instance_", "")
        return f"{repo_base}.{repo_name_only}-{hsh}"
    else:
        image_name = "default"

    # Extract the tag part from the instance ID
    # For UIDs that start with a pattern like "django__django-", extract everything after position 9
    if "__" in uid and len(uid) > 9:
        tag_part = uid[9:]  # Skip the first 9 characters (e.g., "django__")
    else:
        tag_part = uid

    return f"{image_name}-{tag_part}"




def prepare_run(uid, output_dir, prefix, redo):
    uid_dir = os.path.join(output_dir, uid)
    os.makedirs(uid_dir, exist_ok=True)
    output_path = os.path.join(uid_dir, f"{prefix}_output.json")
    if not redo and os.path.exists(output_path):
        print(f"Skipping {uid} - output already exists")
        with open(output_path, "r") as f:
            return json.load(f), output_path, os.path.join(uid_dir, "workspace")
    workspace_dir = os.path.join(uid_dir, "workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    return None, output_path, workspace_dir


def write_patch_snapshot(output_dir, uid, prefix, patch):
    with open(os.path.join(output_dir, uid, f"{prefix}_patch.diff"), "w") as f:
        f.write(patch)


def assemble_workspace_files(uid, scripts_dir, patch, sample):
    run_script = load_local_script(scripts_dir, uid, "run_script.sh")
    parser_script = load_local_script(scripts_dir, uid, "parser.py")
    entryscript_content = create_entryscript(sample)

    cleaned_patch = strip_binary_hunks(patch)
    if cleaned_patch != patch:
        print(f"Stripped binary diff hunks from patch for {uid}")

    files = {
        "patch.diff": cleaned_patch,
        "run_script.sh": run_script,
        "parser.py": parser_script,
        "entryscript.sh": entryscript_content,
    }
    return files, entryscript_content


def write_files_modal(sandbox, files):
    for rel_path, content in files.items():
        with sandbox.open(f"/workspace/{rel_path}", "w") as f:
            f.write(content)


def write_files_local(workspace_dir, files):
    for rel_path, content in files.items():
        dst = os.path.join(workspace_dir, rel_path)
        with open(dst, "w") as f:
            f.write(content)


def save_entryscript_copy(output_dir, uid, prefix, entryscript_content):
    with open(os.path.join(output_dir, uid, f"{prefix}_entryscript.sh"), "w") as f:
        f.write(entryscript_content if entryscript_content is not None else "")


def collect_outputs_modal(sandbox, output_dir, uid, prefix):
    # Save logs first (best-effort)
    try:
        with sandbox.open("/workspace/stdout.log", "r") as f_in:
            with open(os.path.join(output_dir, uid, f"{prefix}_stdout.log"), "w") as f:
                stdout_content = f_in.read()
                f.write(stdout_content if stdout_content is not None else "")
    except FileNotFoundError:
        pass
    try:
        with sandbox.open("/workspace/stderr.log", "r") as f_in:
            with open(os.path.join(output_dir, uid, f"{prefix}_stderr.log"), "w") as f:
                stderr_content = f_in.read()
                f.write(stderr_content if stderr_content is not None else "")
    except FileNotFoundError:
        pass

    # Then try to read output.json
    try:
        with sandbox.open("/workspace/output.json", "r") as f_in:
            output = json.load(f_in)
            with open(os.path.join(output_dir, uid, f"{prefix}_output.json"), "w") as f:
                json.dump(output, f)
            return output
    except FileNotFoundError:
        print(
            f"Warning: output.json not found for {uid}. Check {prefix}_stdout.log and {prefix}_stderr.log for details"
        )
        return None


def collect_outputs_local(workspace_dir, output_dir, uid, prefix):
    def _copy_safe(src_name, dest_name):
        src_path = os.path.join(workspace_dir, src_name)
        dest_path = os.path.join(output_dir, uid, dest_name)
        try:
            with open(src_path, "r") as f_in:
                content = f_in.read()
        except FileNotFoundError:
            content = ""
        with open(dest_path, "w") as f_out:
            f_out.write(content if content is not None else "")

    _copy_safe("stdout.log", f"{prefix}_stdout.log")
    _copy_safe("stderr.log", f"{prefix}_stderr.log")

    # Then try to read output.json
    try:
        with open(os.path.join(workspace_dir, "output.json"), "r") as f_in:
            output = json.load(f_in)
            with open(os.path.join(output_dir, uid, f"{prefix}_output.json"), "w") as f:
                json.dump(output, f)
            return output
    except FileNotFoundError:
        print(
            f"Warning: output.json not found for {uid}. Check {prefix}_stdout.log and {prefix}_stderr.log for details"
        )
        return None


def eval_with_modal(patch, sample, output_dir, dockerhub_username, scripts_dir, prefix="", redo=False, block_network=False, docker_platform=None):
    if modal is None:
        raise RuntimeError("modal is not installed. Install it or run with --use_local_docker")
    uid = sample["instance_id"]
    existing_output, output_path, workspace_dir = prepare_run(uid, output_dir, prefix, redo)
    if existing_output is not None:
        return existing_output

    sandbox = None
    
    print(f"Running evaluation for {uid}")
    try:
        write_patch_snapshot(output_dir, uid, prefix, patch)

        try:
            files, entryscript_content = assemble_workspace_files(uid, scripts_dir, patch, sample)
        except FileNotFoundError as e:
            print(f"Error loading scripts for {uid}: {e}")
            return None

        app = modal.App.lookup(name="swe-bench-pro-eval", create_if_missing=True)
        
        # Use Docker Hub image instead of ECR
        dockerhub_image_uri = get_dockerhub_image_uri(uid, dockerhub_username, sample.get("repo", ""))
        print(f"Using Docker Hub image: {dockerhub_image_uri}")
        
        image = modal.Image.from_registry(
            dockerhub_image_uri
        )

        sandbox = modal.Sandbox.create(
            image=image,
            app=app,
            timeout=60 * 60,
            cpu=(1, 4),
            memory=(5 * 1024, 30 * 1024),
            block_network=block_network,
        )
        
        process = sandbox.exec("mkdir", "-p", "/workspace")
        process.wait()
        
        write_files_modal(sandbox, files)
            
        process = sandbox.exec("bash", "/workspace/entryscript.sh")
        process.wait()
        
        # Check if the process was successful
        if process.returncode != 0:
            print(f"Entryscript failed for {uid} with return code: {process.returncode}")
            # Get stderr from the process directly (note: this may not work with all Modal versions)
            try:
                stderr_content = getattr(process, 'stderr', None)
                if stderr_content and hasattr(stderr_content, 'read'):
                    error_details = stderr_content.read()
                    if error_details:
                        print(f"Error details for {uid}:")
                        print(error_details[:1000])  # Print first 1000 chars
            except Exception as e:
                print(f"Failed to read stderr for {uid}: {e}")
            
        output = collect_outputs_modal(sandbox, output_dir, uid, prefix)
        if output is None:
            return None
        save_entryscript_copy(output_dir, uid, prefix, entryscript_content)
            
        return output
    except Exception as e:
        print(f"Error in eval_with_modal for {uid}: {repr(e)}")
        print(f"Error type: {type(e)}")
        return None
    finally:
        if sandbox:
            try:
                sandbox.terminate()
            except Exception:
                pass


def eval_with_docker(patch, sample, output_dir, dockerhub_username, scripts_dir, prefix="", redo=False, block_network=False, docker_platform=None):
    if docker is None:
        raise RuntimeError("docker SDK is not installed. Install via 'pip install docker' or run without --use_local_docker")
    uid = sample["instance_id"]
    existing_output, output_path, workspace_dir = prepare_run(uid, output_dir, prefix, redo)
    if existing_output is not None:
        return existing_output

    print(f"Running local-docker evaluation for {uid}")

    try:
        try:
            files, entryscript_content = assemble_workspace_files(uid, scripts_dir, patch, sample)
        except FileNotFoundError as e:
            print(f"Error loading scripts for {uid}: {e}")
            return None
        write_files_local(workspace_dir, files)
        write_patch_snapshot(output_dir, uid, prefix, patch)

        # Run container via Docker SDK
        dockerhub_image_uri = get_dockerhub_image_uri(uid, dockerhub_username, sample.get("repo", ""))
        print(f"Using Docker Hub image: {dockerhub_image_uri}")

        client = docker.from_env(timeout=600)
        try:
            client.images.get(dockerhub_image_uri)
            print(f"Using locally cached Docker image: {dockerhub_image_uri}")
        except Exception:
            try:
                if docker_platform:
                    client.images.pull(
                        dockerhub_image_uri, platform=docker_platform
                    )
                else:
                    client.images.pull(dockerhub_image_uri)
            except Exception as pull_err:
                print(f"Failed to pull image for {uid}: {pull_err}")
                return None

        abs_workspace_dir = os.path.abspath(workspace_dir)
        volumes = {abs_workspace_dir: {"bind": "/workspace", "mode": "rw"}}
        run_kwargs = {
            "volumes": volumes,
            "detach": True,
            "remove": True,
            "entrypoint": "/bin/bash",  # Override image entrypoint
            "command": ["-c", "bash /workspace/entryscript.sh"],
        }
        if block_network:
            run_kwargs["network_mode"] = "none"
        # Optional platform override (useful on Apple Silicon)
        if docker_platform:
            run_kwargs["platform"] = docker_platform

        container = client.containers.run(
            dockerhub_image_uri,
            **run_kwargs,
        )

        result = container.wait()
        status_code = result.get("StatusCode", 1) if isinstance(result, dict) else 1
        if status_code != 0:
            print(f"Entryscript failed for {uid} with return code: {status_code}")
        # Collect outputs and logs, and save entryscript for reference
        output = collect_outputs_local(workspace_dir, output_dir, uid, prefix)
        if output is None:
            return None
        save_entryscript_copy(output_dir, uid, prefix, entryscript_content)

        return output
    except Exception as e:
        print(f"Error in eval_with_docker for {uid}: {repr(e)}")
        print(f"Error type: {type(e)}")
        return None


def parse_args():
    parser = argparse.ArgumentParser(description="Run SWEAP Pro evaluations using Modal or local Docker with Docker Hub images and local scripts")
    parser.add_argument("--raw_sample_path", required=True, help="Path to the raw sample CSV file")
    parser.add_argument(
        "--patch_path", required=True, help="Path to the JSON file containing patches"
    )
    parser.add_argument("--output_dir", required=True, help="Directory to store evaluation outputs")
    parser.add_argument(
        "--dockerhub_username", required=True, help="Docker Hub username where sweap-images repository is located"
    )
    parser.add_argument(
        "--scripts_dir", required=True, help="Directory containing local run scripts (e.g., scripts/run_scripts)"
    )
    parser.add_argument(
        "--use_local_docker", action="store_true", help="Run locally with Docker instead of Modal"
    )
    parser.add_argument(
        "--docker_platform",
        default=None,
        help="Docker platform override, e.g., linux/amd64; defaults to auto-detect",
    )
    parser.add_argument(
        "--redo", action="store_true", help="Redo evaluations even if output exists"
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=50,
        help="Number of workers to run evaluations in parallel",
    )
    parser.add_argument(
        "--block_network", action="store_true", help="Block network access inside container"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Support both JSONL and CSV input files
    if args.raw_sample_path.endswith(".jsonl"):
        raw_sample_df = pd.read_json(args.raw_sample_path, lines=True)
    else:
        raw_sample_df = pd.read_csv(args.raw_sample_path)
    
    # Replace nulls with empty strings
    raw_sample_df = raw_sample_df.fillna("")
    
    # use instance_id as index
    raw_sample_df = raw_sample_df.set_index("instance_id", drop=False)

    # each patch sample is a dict with keys: instance_id, patch, prefix
    with open(args.patch_path, "r") as f:
        patches_to_run = json.load(f)
    eval_results = {}

    # Filter patches to only include those with matching instance_ids in the raw sample data
    valid_patches = []
    missing_instances = []
    for patch_sample in patches_to_run:
        instance_id = patch_sample["instance_id"]
        if instance_id in raw_sample_df.index:
            valid_patches.append(patch_sample)
        else:
            missing_instances.append(instance_id)
    
    if missing_instances:
        print(f"Warning: Found {len(missing_instances)} patch instances not in raw sample data:")
        for missing_id in missing_instances[:5]:  # Show first 5
            print(f"  - {missing_id}")
        if len(missing_instances) > 5:
            print(f"  ... and {len(missing_instances) - 5} more")
        print(f"Proceeding with {len(valid_patches)} valid patches out of {len(patches_to_run)} total patches")

    # Select runtime
    # Auto-detect default platform if not provided: prefer linux/amd64 on Apple Silicon
    detected_platform = None
    if args.use_local_docker and args.docker_platform is None:
        try:
            if py_platform.machine().lower() in {"arm64", "aarch64"}:
                detected_platform = "linux/amd64"
        except Exception:
            detected_platform = None

    eval_fn = eval_with_docker if args.use_local_docker else eval_with_modal

    phases = plan_evaluation_phases(valid_patches, args.num_workers)
    if len(phases) > 1:
        regular_count = len(phases[0][1])
        sensitive_count = len(phases[1][1])
        print(
            "Evaluation will run in two sequential phases: "
            f"{regular_count} regular instances with {args.num_workers} workers, "
            f"then {sensitive_count} timing-sensitive instances with "
            f"{TIMING_SENSITIVE_WORKERS} workers."
        )

    # Each executor is closed before the next phase starts, so timing-sensitive
    # instances never overlap with the regular high-concurrency workload.
    for phase_name, phase_patches, phase_workers in phases:
        print(
            f"Starting {phase_name}: {len(phase_patches)} instances with "
            f"{phase_workers} workers"
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=phase_workers
        ) as executor:
            # Create a dictionary mapping futures to their patch samples for progress tracking
            future_to_patch = {
                executor.submit(
                    eval_fn,
                    patch_sample.get("model_patch", patch_sample.get("patch", "")),
                    raw_sample_df.loc[patch_sample["instance_id"]],
                    args.output_dir,
                    args.dockerhub_username,
                    args.scripts_dir,
                    prefix=patch_sample.get("prefix", ""),
                    redo=args.redo,
                    block_network=args.block_network,
                    docker_platform=(args.docker_platform or detected_platform)
                    if args.use_local_docker
                    else None,
                ): patch_sample
                for patch_sample in phase_patches
            }

            # Track progress with tqdm and show running accuracy
            pbar = tqdm(
                concurrent.futures.as_completed(future_to_patch),
                total=len(phase_patches),
                desc=phase_name,
            )
            for future in pbar:
                patch_sample = future_to_patch[future]
                try:
                    # Get the result (if any error occurred, it will be raised here)
                    output = future.result()
                    if output is None:
                        print(
                            f'Evaluation for {patch_sample["instance_id"]} returned None'
                        )
                        eval_results[patch_sample["instance_id"]] = False
                    else:
                        instance_id = patch_sample["instance_id"]
                        if instance_id not in raw_sample_df.index:
                            print(
                                f"Warning: Instance {instance_id} not found in raw "
                                "sample data, skipping"
                            )
                            eval_results[instance_id] = False
                        else:
                            raw_sample = raw_sample_df.loc[instance_id]
                            passed_tests = {
                                x["name"]
                                for x in output["tests"]
                                if x["status"] == "PASSED"
                            }
                            f2p = set(eval(raw_sample["fail_to_pass"]))
                            p2p = set(eval(raw_sample["pass_to_pass"]))
                            result = (f2p | p2p) <= passed_tests
                            eval_results[instance_id] = result

                    current_accuracy = sum(eval_results.values()) / len(eval_results)
                    pbar.set_description(f"Accuracy: {current_accuracy:.2%}")
                except Exception as exc:
                    print(
                        f'Evaluation for {patch_sample["instance_id"]} generated '
                        f"an exception: {exc}"
                    )
                    eval_results[patch_sample["instance_id"]] = False
                    # Update progress bar description with current accuracy
                    current_accuracy = sum(eval_results.values()) / len(eval_results)
                    pbar.set_description(f"Accuracy: {current_accuracy:.2%}")
    with open(os.path.join(args.output_dir, "eval_results.json"), "w") as f:
        json.dump(eval_results, f)
    print("Overall accuracy: ", sum(eval_results.values()) / len(eval_results))


if __name__ == "__main__":
    main()
