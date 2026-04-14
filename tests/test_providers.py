"""Tests for provider abstractions."""
from diffgraph.providers.git_repo import GitRepoProvider, GitAuthConfig


class TestGitAuthConfig:

    def test_defaults(self):
        cfg = GitAuthConfig()
        assert cfg.method == "header"
        assert cfg.ssh_port == 7999

    def test_ssh(self):
        cfg = GitAuthConfig(method="ssh", ssh_port=7998)
        assert cfg.method == "ssh"
        assert cfg.ssh_port == 7998


class TestGitRepoProvider:

    def test_https_to_ssh(self):
        git = GitRepoProvider(GitAuthConfig(method="ssh", ssh_port=7999))
        url = git._https_to_ssh("https://server.com/bitbucket/scm/proj/repo.git")
        assert url == "ssh://git@server.com:7999/proj/repo.git"

    def test_https_to_ssh_custom_port(self):
        git = GitRepoProvider(GitAuthConfig(method="ssh", ssh_port=7998))
        url = git._https_to_ssh("https://server.com/scm/myproj/myrepo.git")
        assert url == "ssh://git@server.com:7998/myproj/myrepo.git"

    def test_https_to_ssh_no_scm(self):
        git = GitRepoProvider(GitAuthConfig(method="ssh"))
        url = git._https_to_ssh("https://server.com/proj/repo.git")
        assert "git@server.com" in url

    def test_cleanup_noop(self):
        git = GitRepoProvider(GitAuthConfig())
        git.cleanup()  # should not raise

    def test_ssl_flags_empty(self):
        git = GitRepoProvider(GitAuthConfig())
        assert git._ssl_flags() == []

    def test_ssl_flags_ca(self):
        git = GitRepoProvider(GitAuthConfig(ca_bundle="/path/ca.pem"))
        flags = git._ssl_flags()
        assert "-c" in flags
        assert "http.sslCAInfo=/path/ca.pem" in flags

    def test_ssl_flags_cert(self):
        git = GitRepoProvider(GitAuthConfig(client_cert="/path/client.pem"))
        flags = git._ssl_flags()
        assert "http.sslCert=/path/client.pem" in flags

    def test_ssl_flags_both(self):
        git = GitRepoProvider(GitAuthConfig(ca_bundle="/ca.pem", client_cert="/cl.pem"))
        flags = git._ssl_flags()
        assert len(flags) == 4  # -c, ca, -c, cert
