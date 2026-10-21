{{/* The chart's name, truncated to the 63 characters a label allows. */}}
{{- define "earshot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* The release's resource name. */}}
{{- define "earshot.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "earshot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "earshot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "earshot.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "earshot.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "earshot.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{/*
Settings that would lose data or open the API are refused at render time:
- the operator token is required, Ingress or not;
- SQLite and file recordings need the data volume to outlive the pod.
*/}}
{{- define "earshot.validate" -}}
{{- if not .Values.auth.existingSecret }}
{{- fail "auth.existingSecret is required: name a Secret that holds the operator token (see docs/install-helm.md). The chart never serves the API without one." }}
{{- end }}
{{- if and (not .Values.persistence.enabled) (not .Values.database.existingSecret) }}
{{- fail "persistence.enabled=false needs database.existingSecret: a SQLite catalog on an emptyDir is deleted with the pod." }}
{{- end }}
{{- if and (not .Values.persistence.enabled) (not .Values.blobs.url) }}
{{- fail "persistence.enabled=false needs blobs.url: recordings on an emptyDir are deleted with the pod." }}
{{- end }}
{{- end }}
