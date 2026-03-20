class DarwinPerf < Formula
  desc "System performance monitoring + IDS for macOS Apple Silicon"
  homepage "https://github.com/adammikulis/darwin-perf"
  url "https://github.com/adammikulis/darwin-perf/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on :macos
  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources

    # Generate shell completion
    generate_completions_from_executable(bin/"darwin-perf", shells: [:bash, :zsh])
  end

  def caveats
    <<~EOS
      darwin-perf is installed! Quick start:

        darwin-perf              # live GPU/CPU monitor
        darwin-perf --tui        # rich terminal UI
        darwin-perf --ids        # intrusion detection
        darwin-perf --menubar    # menu bar app

      To run IDS as a background daemon:
        darwin-perf --ids-install

      Python API:
        import darwin_perf as dp
        s = dp.stats()
    EOS
  end

  test do
    output = shell_output("#{bin}/darwin-perf --json -n 1")
    assert_match "timestamp", output
    assert_match "processes", output
  end
end
