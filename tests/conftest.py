"""app 모듈은 import할 때 DATA_DIR(sqlite 위치)을 읽으므로 테스트 전용 빈 폴더를 먼저 지정한다."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kcs-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
