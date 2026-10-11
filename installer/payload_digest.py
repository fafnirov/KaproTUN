"""SHA-256 of the KaproTUN.exe this installer was built together with.

The installer does not carry the application: it downloads KaproTUN.exe of
its own version at install time. This is what it compares the download with.

The release build fills the value in between building KaproTUN.exe and
building the installer (packaging/write_payload_digest.py), so the pair is
bound: this installer installs that exact binary and no other.

Empty here, in the repository — an installer built by hand has no pair. It
then asks the GitHub release for the digest (operations._release_digest) and
refuses to install if it cannot get one.
"""
MAIN_EXE_SHA256 = ""
