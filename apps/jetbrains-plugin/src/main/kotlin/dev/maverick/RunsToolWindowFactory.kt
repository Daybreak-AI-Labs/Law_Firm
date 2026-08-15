package dev.maverick

import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import java.net.HttpURLConnection
import java.net.URI
import java.util.concurrent.atomic.AtomicBoolean
import javax.swing.JButton
import javax.swing.JPanel
import javax.swing.JTextField
import kotlin.concurrent.thread

/**
 * Lightwork Runs tool window: enter a goal id, stream its events live from
 * the local dashboard's SSE endpoint into the text area. Reconnect/backoff
 * mirrors the VS Code extension; the watch stops when the window closes or
 * Stop is pressed.
 */
class RunsToolWindowFactory : ToolWindowFactory {
    /**
     * The bearer may only be attached when the transport protects it: HTTPS, or
     * a loopback host where it never leaves the machine. Over plain http:// to a
     * non-loopback host the token would travel in cleartext. Mirrors the VS Code
     * extension / mobile-companion's transport-safety gate.
     */
    private fun isTokenTransportSafe(uri: URI): Boolean {
        if (uri.scheme.equals("https", ignoreCase = true)) return true
        val host = (uri.host ?: "").trim().lowercase()
        return host == "127.0.0.1" || host == "localhost" || host == "::1" || host == "[::1]"
    }

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val output = JBTextArea().apply { isEditable = false }
        val goalField = JTextField(8)
        val watch = JButton("Watch live")
        val stop = JButton("Stop")
        // The CURRENT watch's stop flag. Each "Watch live" click installs a fresh
        // flag; a shared flag let a second click spawn a second concurrent SSE
        // thread that the first thread's loop could never be told apart from --
        // and Stop could not target one of them.
        val activeStop = java.util.concurrent.atomic.AtomicReference<AtomicBoolean?>(null)

        fun appendLine(line: String) =
            javax.swing.SwingUtilities.invokeLater { output.append(line + "\n") }

        watch.addActionListener {
            val goalId = goalField.text.trim().toLongOrNull() ?: return@addActionListener
            // Stop any prior watch (its own flag) before starting this one, so we
            // never accumulate concurrent threads writing to the same output.
            activeStop.getAndSet(null)?.set(true)
            val myStop = AtomicBoolean(false)
            activeStop.set(myStop)
            thread(isDaemon = true, name = "maverick-sse-$goalId") {
                var backoffMs = 1000L
                val base = System.getenv("MAVERICK_DASHBOARD_URL") ?: "http://127.0.0.1:8765"
                val token = System.getenv("MAVERICK_DASHBOARD_TOKEN")
                while (!myStop.get()) {
                    try {
                        val uri = URI("$base/api/v1/goals/$goalId/events/stream")
                        val conn = uri.toURL().openConnection() as HttpURLConnection
                        if (!token.isNullOrBlank()) {
                            if (isTokenTransportSafe(uri)) {
                                conn.setRequestProperty("Authorization", "Bearer $token")
                            } else {
                                appendLine("[refusing to send dashboard token over insecure transport " +
                                    "($base); use https:// or a loopback host]")
                            }
                        }
                        conn.inputStream.bufferedReader().useLines { lines ->
                            backoffMs = 1000L
                            lines.forEach { line ->
                                if (myStop.get()) return@forEach
                                if (line.startsWith("data:")) appendLine(line.removePrefix("data:").trim())
                            }
                        }
                    } catch (e: Exception) {
                        appendLine("[stream error: ${e.message}; retrying in ${backoffMs / 1000}s]")
                    }
                    if (!myStop.get()) {
                        Thread.sleep(backoffMs)
                        backoffMs = (backoffMs * 2).coerceAtMost(30_000L)
                    }
                }
                appendLine("[live watch stopped]")
            }
        }
        stop.addActionListener { activeStop.getAndSet(null)?.set(true) }

        val controls = JPanel().apply { add(goalField); add(watch); add(stop) }
        val panel = JPanel(java.awt.BorderLayout()).apply {
            add(controls, java.awt.BorderLayout.NORTH)
            add(JBScrollPane(output), java.awt.BorderLayout.CENTER)
        }
        toolWindow.contentManager.addContent(
            toolWindow.contentManager.factory.createContent(panel, "", false))
    }
}
