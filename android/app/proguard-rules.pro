# kotlinx.serialization
-keepattributes *Annotation*, InnerClasses
-keepclassmembers class kotlinx.serialization.json.** {
    *** Companion;
}
-keepclasseswithmembers class kotlinx.serialization.json.** {
    kotlinx.serialization.KSerializer serializer(...);
}

# Keep models that get serialized (we'll annotate them with @Serializable)
-keep,includedescriptorclasses class pro.kaprovpn.android.core.**$$serializer { *; }
-keepclassmembers class pro.kaprovpn.android.core.** {
    *** Companion;
}
-keepclasseswithmembers class pro.kaprovpn.android.core.** {
    kotlinx.serialization.KSerializer serializer(...);
}
