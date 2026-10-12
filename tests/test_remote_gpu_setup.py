from solution_agent.orchestrator.core import _gpu_setup_script
from solution_agent.orchestrator.schema import TaskBook


def test_gpu_setup_accepts_non_a100_allowed_gpus():
    task = TaskBook(
        task_id="gpu_probe",
        title="GPU probe",
        raw_front_matter={"resource_hint": "gpu", "min_free_gpu_mem_gb": 8},
    )

    lines, candidates, _policy, _semantics, min_free = _gpu_setup_script(
        task,
        gpu_candidates_override=["3"],
        min_free_gpu_mem_gb_override=8,
    )
    script = "\n".join(lines)

    assert candidates == "3"
    assert min_free == 8
    assert "/A100/" not in script
    assert "if (free_gb < min_free_gb) next;" in script
