# Use a lightweight Python image
FROM python:3.9-slim

# Set Python to run in unbuffered mode. This ensures that print statements
# are sent directly to the terminal without being buffered, which is essential for logging.
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file from the src folder and install dependencies
# Paths are relative to the build context (the project's root directory)
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code from the src folder
COPY src/app.py .

# Copy the entrypoint script from the docker folder
COPY docker/entrypoint.sh .
# Make the entrypoint script executable
RUN chmod +x entrypoint.sh

# Expose the port Gunicorn will run on
EXPOSE 8080

# Set the entrypoint to our script.
# This will be executed when the container starts.
CMD ["./entrypoint.sh"]


