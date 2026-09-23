from unittest.mock import Mock
from InferenceNode.pipeline import InferencePipeline

def test_manual_reenable_clears_failure_latch_and_frame_pause():
    pipeline = InferencePipeline()
    destination = Mock(enabled=False, frame_limit_reached=True)
    pipeline.result_publisher = Mock(destinations=[destination])
    pipeline.result_publisher.get_by_id.return_value = destination
    pipeline.enable_publisher('webhook-test')
    destination.reset_failure_count.assert_called_once()
    destination.reset_frame_count.assert_called_once()
    assert destination.enabled is True
