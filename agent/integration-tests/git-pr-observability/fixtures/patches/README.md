# Patch fixtures

Most patches are generated from the temporary Git repository so their base SHA and
binary representation are real. Tests mutate the generated raw bytes for digest,
manifest, protected-path, and scope cases. No delivery decision is implemented here.
