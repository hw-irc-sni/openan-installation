{{- define "rc.componentName" -}}{{ .Release.Name }}-{{ .Values.component.name }}{{- end -}}
{{- define "rc.fullname" -}}{{ .Release.Name }}-registry-center{{- end -}}
{{- /* With tmf639.enabled the component identifies as TMFC012 Resource Inventory unless overridden. */}}
{{- define "rc.componentId" -}}{{ .Values.component.id | default (ternary "TMFC012" "OPENAN-RC" (eq (toString .Values.tmf639.enabled) "true")) }}{{- end -}}
{{- define "rc.functionalBlock" -}}{{ .Values.component.functionalBlock | default (ternary "Production" "AIAgentRegistry" (eq (toString .Values.tmf639.enabled) "true")) }}{{- end -}}
{{- define "rc.tmf639" -}}{{ eq (toString .Values.tmf639.enabled) "true" }}{{- end -}}
{{- define "rc.broadcast" -}}{{ eq (toString .Values.broadcast.enabled) "true" }}{{- end -}}
{{- define "rc.catalog" -}}{{ eq (toString .Values.catalog.enabled) "true" }}{{- end -}}
{{- define "rc.gatewayConfiguration" -}}
apiKeyVerification: {{ .Values.component.apipolicy.apiKeyVerification | toYaml | nindent 2 }}
rateLimit: {{ .Values.component.apipolicy.rateLimit | toYaml | nindent 2 }}
quota: {{ .Values.component.apipolicy.quota | toYaml | nindent 2 }}
OASValidation: {{ .Values.component.apipolicy.OASValidation | toYaml | nindent 2 }}
CORS: {{ .Values.component.apipolicy.CORS | toYaml | nindent 2 }}
template: {{ .Values.component.apipolicy.template | quote }}
{{- end -}}
{{- define "rc.labels" -}}
app: {{ include "rc.fullname" . }}
oda.tmforum.org/componentName: {{ include "rc.componentName" . }}
app.kubernetes.io/name: openan-registry-center
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
