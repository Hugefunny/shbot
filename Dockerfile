FROM erispulse/erispulse:latest

# Install the shbot module
COPY shbot/ /tmp/shbot/shbot/
COPY pyproject.toml /tmp/shbot/
RUN pip install /tmp/shbot && rm -rf /tmp/shbot