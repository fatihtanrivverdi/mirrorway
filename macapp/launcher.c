/*
 * Mirrorway.app launcher.
 *
 * macOS permissions (Accessibility, Input Monitoring, Screen Recording) are granted to the
 * "responsible process". This small program is the app's main executable and starts Python as a
 * CHILD PROCESS instead of replacing itself with it (no exec); so the permissions belong only to
 * Mirrorway.app, not to Terminal or Python.
 */
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

/* drop the last path component: /a/b/c -> /a/b */
static void parent_dir(char *p) {
    char *s = strrchr(p, '/');
    if (s && s != p) *s = '\0';
}

int main(void) {
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) return 1;

    char real[PATH_MAX];
    if (!realpath(exe, real)) {
        strncpy(real, exe, sizeof(real) - 1);
        real[sizeof(real) - 1] = '\0';
    }
    char contents[PATH_MAX];
    strncpy(contents, real, sizeof(contents) - 1);
    contents[sizeof(contents) - 1] = '\0';
    parent_dir(contents);           /* .../Contents/MacOS */
    parent_dir(contents);           /* .../Contents       */
    char bundle[PATH_MAX];
    strncpy(bundle, contents, sizeof(bundle) - 1);
    bundle[sizeof(bundle) - 1] = '\0';
    parent_dir(bundle);             /* .../Mirrorway.app       */

    char script[PATH_MAX];
    snprintf(script, sizeof(script), "%s/Resources/start.sh", contents);

    setenv("MIRRORWAY_APP", "1", 1);
    setenv("MIRRORWAY_BUNDLE", bundle, 1);

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = forward;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    char *argv[] = {"/bin/bash", script, NULL};
    pid_t pid;
    if (posix_spawn(&pid, "/bin/bash", NULL, NULL, argv, environ) != 0) return 1;
    child = pid;

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
