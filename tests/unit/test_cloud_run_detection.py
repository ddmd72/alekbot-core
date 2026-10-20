from src.utils.cloud_run import on_cloud_run


def test_service_or_job_env_means_cloud_run():
    assert on_cloud_run({"K_SERVICE": "s"}.get)
    assert on_cloud_run({"CLOUD_RUN_JOB": "j"}.get)
    assert not on_cloud_run({"K_JOB": "j"}.get)
    assert not on_cloud_run({}.get)
