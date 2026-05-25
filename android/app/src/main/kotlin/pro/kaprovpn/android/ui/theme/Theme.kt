package pro.kaprovpn.android.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val DarkScheme = darkColorScheme(
    primary = AmberAccent,
    onPrimary = Color.Black,
    secondary = AmberAccentDark,
    background = DarkBackground,
    surface = DarkSurface,
    surfaceVariant = DarkSurfaceElevated,
    onBackground = DarkOnSurface,
    onSurface = DarkOnSurface,
    onSurfaceVariant = DarkOnSurfaceMuted,
)

@Composable
fun KaproVpnTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = DarkScheme, content = content)
}
