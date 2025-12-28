pipeline {
  // Use a general agent; we will run build/test inside a Docker container manually
  agent any

  options {
    timeout(time: 20, unit: 'MINUTES')
  }

  environment {
    PIP_CACHE_DIR = '.cache/pip'
    PYTHONDONTWRITEBYTECODE = '1'
    PYTHONUNBUFFERED = '1'
    PIP_DEFAULT_TIMEOUT = '120'
    PIP_DISABLE_PIP_VERSION_CHECK = '1'
    // Faster mirror for China
    PIP_INDEX_URL = 'https://pypi.tuna.tsinghua.edu.cn/simple'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Install Dependencies') {
      steps {
        sh '''
          if docker info >/dev/null 2>&1; then
            docker run --rm \
              -u $(id -u):$(id -g) \
              -v "$PWD":/app -w /app \
              -e PIP_CACHE_DIR=${PIP_CACHE_DIR} \
              -e PIP_INDEX_URL=${PIP_INDEX_URL} \
              python:3.12-slim \
              sh -c "python -m pip install --upgrade pip && pip install -r requirements.txt"
          else
            echo "Docker unavailable or permission denied; running natively" >&2
            export PATH="$HOME/.local/bin:$PATH"
            PYTHON_BIN="$(command -v python3 || command -v python || true)"
            if [ -z "$PYTHON_BIN" ]; then
              sudo apt-get update && sudo apt-get install -y python3 python3-pip || true
              PYTHON_BIN="$(command -v python3 || command -v python)"
            fi
            "$PYTHON_BIN" -m pip install --upgrade pip --index-url ${PIP_INDEX_URL}
            "$PYTHON_BIN" -m pip install -r requirements.txt --index-url ${PIP_INDEX_URL}
          fi
        '''
      }
    }

    stage('Unit Tests') {
      steps {
        // Match GitHub Actions: run unit tests and produce JUnit XML
        sh '''
          if docker info >/dev/null 2>&1; then
            docker run --rm \
              -u $(id -u):$(id -g) \
              -v "$PWD":/app -w /app \
              -e PIP_CACHE_DIR=${PIP_CACHE_DIR} \
              -e PIP_INDEX_URL=${PIP_INDEX_URL} \
              python:3.12-slim \
              sh -c "pip install pytest && pytest -q tests/unit -m unit --junitxml=pytest-unit-report.xml"
          else
            echo "Docker unavailable or permission denied; running tests natively" >&2
            export PATH="$HOME/.local/bin:$PATH"
            PYTHON_BIN="$(command -v python3 || command -v python || true)"
            if [ -z "$PYTHON_BIN" ]; then
              sudo apt-get update && sudo apt-get install -y python3 python3-pip || true
              PYTHON_BIN="$(command -v python3 || command -v python)"
            fi
            "$PYTHON_BIN" -m pip install pytest --index-url ${PIP_INDEX_URL}
            "$PYTHON_BIN" -m pytest -q tests/unit -m unit --junitxml=pytest-unit-report.xml
          fi
        '''
      }
      post {
        always {
          junit allowEmptyResults: true, testResults: 'pytest-unit-report.xml'
          archiveArtifacts artifacts: 'pytest-unit-report.xml', fingerprint: true, allowEmptyArchive: true
        }
      }
    }

    stage('Integration Tests (skipped)') {
      steps {
        echo 'Skipping integration tests (matches GitHub Actions).' 
      }
    }

    // stage('Deploy to VolcEngine') {
    //   when {
    //     branch 'main'
    //   }
    //   steps {
    //     // Requires Jenkins credential ID 'volc_ssh_key' and env vars VOLC_USER/VOLC_HOST
    //     sshagent(credentials: ['volc_ssh_key']) {
    //       sh '''
    //         ssh -o StrictHostKeyChecking=no ${VOLC_USER}@${VOLC_HOST} <<'EOF'
    //           cd /home/shuyolin/quantFinance
    //           git pull
    //           sudo systemctl restart uvicorn
    //         EOF
    //       '''
    //     }
    //   }
    // }
  }

  post {
    cleanup {
      sh 'pip cache purge || true'
    }
  }
}