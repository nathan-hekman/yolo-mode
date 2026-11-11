// routine-runner: launchd's program for jobs that need ~/Documents.
// macOS privacy (TCC) judges a launchd job by its program. /bin/bash has no
// Documents access, and granting it would hand every script full disk access.
// This binary gets Full Disk Access once (System Settings), spawns its
// arguments as a child (not exec, so it stays the responsible process), waits,
// and returns the child's exit status. Do not rebuild casually: a new binary
// is a new identity and loses the grant.
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
extern char **environ;
int main(int argc, char **argv) {
  if (argc < 2) { fprintf(stderr, "usage: routine-runner <program> [args...]\n"); return 64; }
  pid_t pid;
  int rc = posix_spawnp(&pid, argv[1], NULL, NULL, argv + 1, environ);
  if (rc != 0) { perror("posix_spawnp"); return 127; }
  int status;
  if (waitpid(pid, &status, 0) < 0) { perror("waitpid"); return 1; }
  return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
